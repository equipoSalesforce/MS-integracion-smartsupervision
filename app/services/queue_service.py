# app/services/queue_service.py
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy import delete, func
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cola_model import ColaDespachoModel
from app.core.config import settings
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)


class QueueService:
    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def contar_pendientes(self) -> int:
        """Obtiene el número total de casos en estado PENDIENTE."""
        stmt = select(func.count(ColaDespachoModel.id)).where(ColaDespachoModel.estado == "PENDIENTE")
        result = await self.db.execute(stmt)
        return result.scalar() or 0

    async def encolar_despacho(
        self, 
        smart_code: str, 
        tipo_operacion: str, 
        payload_json: Dict[str, Any], 
        error_inicial: str
    ) -> ColaDespachoModel:
        """Registra un nuevo caso en la cola local tras detectar falla de red o servidor en la SFC."""
        
        # 1. Contamos cuántos casos pendientes existían ANTES de guardar el actual
        pendientes_previos = await self.contar_pendientes()

        item = ColaDespachoModel(
            smart_code=smart_code,
            tipo_operacion=tipo_operacion,
            payload_json=payload_json,
            estado="PENDIENTE",
            intentos=1,
            max_intentos=settings.QUEUE_MAX_RETRIES,
            ultimo_error=error_inicial,
            proximo_reintento_at=datetime.utcnow() + timedelta(minutes=settings.QUEUE_RETRY_INTERVAL_MINUTES)
        )
        self.db.add(item)
        await self.db.commit()
        await self.db.refresh(item)
        logger.warning(f"📦 [Cola SQLite] Caso {smart_code} encolado para reintento automático. Registro ID: {item.id}")

        # 🚨 2. Solo notificar la caída si la cola ESTABA VACÍA (es la primera falla del corte)
        if pendientes_previos == 0:
            logger.info(f"🚨 [QueueService] Primer caso encolado ({smart_code}). Notificando caída de infraestructura.")
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code=smart_code,
                error_msg=error_inicial
            )

        # 📊 3. Evaluación de Umbral de Volumen (Cada 100 casos acumulados)
        total_pendientes = pendientes_previos + 1
        if total_pendientes > 0 and total_pendientes % 100 == 0:
            logger.warning(f"📊 [Cola SQLite] Se alcanzó el umbral de acumulados: {total_pendientes} casos.")
            await EmailAlertService.notificar_umbral_cola(total_pendientes=total_pendientes)

        return item

    async def obtener_casos_vencidos_sla(self, horas_limite: int = 12) -> List[Dict[str, Any]]:
        """Obtiene datos formateados de los casos que llevan más de N horas retenidos en PENDIENTE."""
        limite_tiempo = datetime.utcnow() - timedelta(hours=horas_limite)
        stmt = (
            select(ColaDespachoModel)
            .where(
                ColaDespachoModel.estado == "PENDIENTE",
                ColaDespachoModel.created_at <= limite_tiempo
            )
            .order_by(ColaDespachoModel.created_at.asc())
        )
        result = await self.db.execute(stmt)
        registros = result.scalars().all()

        now = datetime.utcnow()
        casos_vencidos = []
        for r in registros:
            # Calcular horas transcurridas desde el encolado
            created_naive = r.created_at.replace(tzinfo=None) if r.created_at else now
            horas_en_cola = (now - created_naive).total_seconds() / 3600.0

            casos_vencidos.append({
                "smart_code": r.smart_code,
                "fecha_encolado": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "N/A",
                "horas_en_cola": horas_en_cola,
                "reintentos": r.intentos,
                "ultimo_error": r.ultimo_error or "Sin detalle de error"
            })

        return casos_vencidos

    async def obtener_pendientes_para_reintento(self) -> List[ColaDespachoModel]:
        """Obtiene los casos pendientes cuyo tiempo de reintento ya venció."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(ColaDespachoModel)
            .where(
                ColaDespachoModel.estado == "PENDIENTE",
                ColaDespachoModel.proximo_reintento_at <= now,
                ColaDespachoModel.intentos < ColaDespachoModel.max_intentos
            )
            .order_by(ColaDespachoModel.id.asc())
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def marcar_exitoso(self, registro_id: int):
        """Marca un registro como entregado con éxito a la SFC."""
        item = await self.db.get(ColaDespachoModel, registro_id)
        if item:
            item.estado = "EXITOSO"
            item.updated_at = datetime.utcnow()
            await self.db.commit()

    async def registrar_fallo(self, registro_id: int, error_msg: str):
        """Suma un intento y recalcula el tiempo del próximo reintento (Backoff)."""
        item = await self.db.get(ColaDespachoModel, registro_id)
        if item:
            item.intentos += 1
            item.ultimo_error = error_msg

            if item.intentos >= item.max_intentos:
                item.estado = "FALLIDO_DEFINITIVO"
                logger.error(f"❌ [Cola SQLite] Caso {item.smart_code} alcanzó el límite máximo de {item.max_intentos} reintentos.")
            else:
                espera_minutos = settings.QUEUE_RETRY_INTERVAL_MINUTES * item.intentos
                item.proximo_reintento_at = datetime.utcnow() + timedelta(minutes=espera_minutos)

            item.updated_at = datetime.utcnow()
            await self.db.commit()

    async def obtener_todos_los_encolados(self, estado: Optional[str] = None) -> List[ColaDespachoModel]:
        """Obtiene los registros de la cola, opcionalmente filtrados por estado."""
        stmt = select(ColaDespachoModel)
        if estado:
            stmt = stmt.where(ColaDespachoModel.estado == estado.upper())
        stmt = stmt.order_by(ColaDespachoModel.created_at.desc())

        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def purgar_registros_antiguos(self, dias_retencion: int = 7) -> int:
        """Elimina registros en estado 'EXITOSO' con más de N días de antigüedad."""
        limite_fecha = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=dias_retencion)
        stmt = (
            delete(ColaDespachoModel)
            .where(
                ColaDespachoModel.estado == "EXITOSO",
                ColaDespachoModel.updated_at <= limite_fecha
            )
        )
        resultado = await self.db.execute(stmt)
        await self.db.commit()

        registros_eliminados = resultado.rowcount
        if registros_eliminados > 0:
            logger.info(f"🧹 [Cola SQLite] Purga completada: {registros_eliminados} registros antiguos eliminados.")

        return registros_eliminados