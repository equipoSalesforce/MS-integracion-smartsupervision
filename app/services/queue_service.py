import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy import delete
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cola_model import ColaDespachoModel
from app.core.config import settings

logger = logging.getLogger(__name__)

class QueueService:
    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def encolar_despacho(self, smart_code: str, tipo_operacion: str, payload_json: Dict[str, Any], error_inicial: str) -> ColaDespachoModel:
        """Registra un nuevo caso en la cola local tras detectar falla de red o servidor en la SFC."""
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
        return item

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
                # Escalado de espera en minutos (Backoff)
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
        """
        Elimina registros en estado 'EXITOSO' que tengan más de N días de antigüedad.
        Devuelve la cantidad de registros eliminados.
        """
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