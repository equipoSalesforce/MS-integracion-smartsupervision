# app/services/queue_service.py
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)


class ColaItemRedis:
    """
    Representación del objeto de registro en la cola Redis,
    manteniendo atributos idénticos al modelo de BD previo.
    """
    def __init__(self, data: dict):
        self.id = int(data.get("id")) if data.get("id") else None
        self.smart_code = str(data.get("smart_code", ""))
        self.tipo_operacion = str(data.get("tipo_operacion", "AUTO"))
        self.payload_json = data.get("payload_json", {})
        self.estado = str(data.get("estado", "PENDIENTE"))
        self.intentos = int(data.get("intentos", 0))
        self.max_intentos = int(data.get("max_intentos", settings.QUEUE_MAX_RETRIES))
        self.ultimo_error = data.get("ultimo_error")
        self.proximo_reintento_at = data.get("proximo_reintento_at")
        self.created_at = data.get("created_at")
        self.updated_at = data.get("updated_at")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "smart_code": self.smart_code,
            "tipo_operacion": self.tipo_operacion,
            "payload_json": self.payload_json,
            "estado": self.estado,
            "intentos": self.intentos,
            "max_intentos": self.max_intentos,
            "ultimo_error": self.ultimo_error,
            "proximo_reintento_at": self.proximo_reintento_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class QueueService:
    """
    Servicio de Cola Centralizada sobre Redis.
    Garantiza concurrencia segura y rendimiento entre múltiples contenedores
    manteniendo paridad con la lógica de contingencia.
    """

    def __init__(self, redis_client=None):
        self.redis = redis_client

    async def contar_pendientes(self) -> int:
        """Obtiene el número total de casos en estado PENDIENTE desde Redis."""
        if not self.redis:
            return 0
        try:
            return await self.redis.scard("sfc:queue:status:PENDIENTE")
        except Exception as e:
            logger.error(f"Error contando pendientes en Redis: {e}")
            return 0

    async def encolar_despacho(
        self, 
        smart_code: str, 
        tipo_operacion: str, 
        payload_json: Dict[str, Any], 
        error_inicial: str
    ) -> ColaItemRedis:
        """Registra un nuevo caso en Redis tras detectar falla de red o servidor en la SFC."""
        if not self.redis:
            logger.error("❌ [Cola Redis] Cliente de Redis no inicializado.")
            raise RuntimeError("Cliente de Redis no disponible.")

        pendientes_previos = await self.contar_pendientes()

        # Generar ID autoincremental en Redis
        item_id = await self.redis.incr("sfc:queue:counter")

        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        proximo_reintento = now_bogota + timedelta(minutes=settings.QUEUE_RETRY_INTERVAL_MINUTES)

        item_dict = {
            "id": item_id,
            "smart_code": smart_code,
            "tipo_operacion": tipo_operacion,
            "payload_json": payload_json,
            "estado": "PENDIENTE",
            "intentos": 1,
            "max_intentos": settings.QUEUE_MAX_RETRIES,
            "ultimo_error": error_inicial,
            "proximo_reintento_at": proximo_reintento.isoformat(),
            "created_at": now_bogota.isoformat(),
            "updated_at": now_bogota.isoformat()
        }

        item_key = f"sfc:queue:item:{item_id}"
        await self.redis.set(item_key, json.dumps(item_dict, ensure_ascii=False))

        # Indexación en Sets y Sorted Sets
        await self.redis.sadd("sfc:queue:status:PENDIENTE", str(item_id))
        await self.redis.zadd("sfc:queue:pending_zset", {str(item_id): proximo_reintento.timestamp()})
        await self.redis.zadd("sfc:queue:created_zset", {str(item_id): now_bogota.timestamp()})

        item_obj = ColaItemRedis(item_dict)
        logger.warning(f"📦 [Cola Redis] Caso {smart_code} encolado para reintento automático. Registro ID: {item_id}")

        # Notificaciones de Alertas por Correo
        if pendientes_previos == 0:
            logger.info(f"🚨 [QueueService Redis] Primer caso encolado ({smart_code}). Notificando caída de infraestructura.")
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code=smart_code,
                error_msg=error_inicial
            )

        total_pendientes = pendientes_previos + 1
        if total_pendientes > 0 and total_pendientes % 100 == 0:
            logger.warning(f"📊 [Cola Redis] Se alcanzó el umbral de acumulados: {total_pendientes} casos.")
            await EmailAlertService.notificar_umbral_cola(total_pendientes=total_pendientes)

        return item_obj

    async def obtener_casos_vencidos_sla(self, horas_limite: int = 12) -> List[Dict[str, Any]]:
        """Obtiene datos formateados de los casos que llevan más de N horas retenidos en PENDIENTE."""
        if not self.redis:
            return []

        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        limite_ts = (now_bogota - timedelta(hours=horas_limite)).timestamp()

        try:
            item_ids = await self.redis.zrangebyscore("sfc:queue:created_zset", "-inf", limite_ts)
            casos_vencidos = []

            for item_id in item_ids:
                is_pending = await self.redis.sismember("sfc:queue:status:PENDIENTE", str(item_id))
                if not is_pending:
                    continue

                raw_item = await self.redis.get(f"sfc:queue:item:{item_id}")
                if not raw_item:
                    continue

                data = json.loads(raw_item)
                created_dt = datetime.fromisoformat(data["created_at"])
                horas_en_cola = (now_bogota - created_dt).total_seconds() / 3600.0

                casos_vencidos.append({
                    "smart_code": data["smart_code"],
                    "fecha_encolado": created_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "horas_en_cola": horas_en_cola,
                    "reintentos": data["intentos"],
                    "ultimo_error": data.get("ultimo_error") or "Sin detalle de error"
                })

            return casos_vencidos
        except Exception as e:
            logger.error(f"Error consultando casos vencidos SLA en Redis: {e}")
            return []

    async def obtener_pendientes_para_reintento(self) -> List[ColaItemRedis]:
        """Obtiene los casos pendientes cuyo tiempo de reintento ya venció."""
        if not self.redis:
            return []

        now_ts = datetime.now(ZoneInfo("America/Bogota")).timestamp()
        try:
            item_ids = await self.redis.zrangebyscore("sfc:queue:pending_zset", "-inf", now_ts)
            pendientes = []

            for item_id in item_ids:
                is_pending = await self.redis.sismember("sfc:queue:status:PENDIENTE", str(item_id))
                if not is_pending:
                    continue

                raw_item = await self.redis.get(f"sfc:queue:item:{item_id}")
                if not raw_item:
                    continue

                data = json.loads(raw_item)
                if data.get("intentos", 0) < data.get("max_intentos", settings.QUEUE_MAX_RETRIES):
                    pendientes.append(ColaItemRedis(data))

            return pendientes
        except Exception as e:
            logger.error(f"Error obteniendo pendientes para reintento en Redis: {e}")
            return []

    async def marcar_exitoso(self, registro_id: int):
        """Marca un registro como entregado con éxito a la SFC."""
        if not self.redis:
            return

        item_key = f"sfc:queue:item:{registro_id}"
        try:
            raw_item = await self.redis.get(item_key)
            if not raw_item:
                return

            data = json.loads(raw_item)
            data["estado"] = "EXITOSO"
            data["updated_at"] = datetime.now(ZoneInfo("America/Bogota")).isoformat()

            await self.redis.set(item_key, json.dumps(data, ensure_ascii=False))
            await self.redis.srem("sfc:queue:status:PENDIENTE", str(registro_id))
            await self.redis.sadd("sfc:queue:status:EXITOSO", str(registro_id))
            await self.redis.zrem("sfc:queue:pending_zset", str(registro_id))
        except Exception as e:
            logger.error(f"Error marcando exitoso registro {registro_id} en Redis: {e}")

    async def registrar_fallo(self, registro_id: int, error_msg: str):
        """Suma un intento y recalcula el tiempo del próximo reintento (Backoff)."""
        if not self.redis:
            return

        item_key = f"sfc:queue:item:{registro_id}"
        try:
            raw_item = await self.redis.get(item_key)
            if not raw_item:
                return

            data = json.loads(raw_item)
            data["intentos"] += 1
            data["ultimo_error"] = error_msg
            now_bogota = datetime.now(ZoneInfo("America/Bogota"))

            if data["intentos"] >= data.get("max_intentos", settings.QUEUE_MAX_RETRIES):
                data["estado"] = "FALLIDO_DEFINITIVO"
                await self.redis.srem("sfc:queue:status:PENDIENTE", str(registro_id))
                await self.redis.sadd("sfc:queue:status:FALLIDO_DEFINITIVO", str(registro_id))
                await self.redis.zrem("sfc:queue:pending_zset", str(registro_id))
                logger.error(f"❌ [Cola Redis] Caso {data['smart_code']} alcanzó el límite máximo de {data['max_intentos']} reintentos.")
            else:
                espera_minutos = settings.QUEUE_RETRY_INTERVAL_MINUTES * data["intentos"]
                proximo_at = now_bogota + timedelta(minutes=espera_minutos)
                data["proximo_reintento_at"] = proximo_at.isoformat()
                await self.redis.zadd("sfc:queue:pending_zset", {str(registro_id): proximo_at.timestamp()})

            data["updated_at"] = now_bogota.isoformat()
            await self.redis.set(item_key, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.error(f"Error registrando fallo para registro {registro_id} en Redis: {e}")

    async def obtener_todos_los_encolados(self, estado: Optional[str] = None) -> List[ColaItemRedis]:
        """Obtiene los registros de la cola, opcionalmente filtrados por estado."""
        if not self.redis:
            return []

        try:
            if estado:
                set_key = f"sfc:queue:status:{estado.upper()}"
                item_ids = await self.redis.smembers(set_key)
            else:
                item_keys = await self.redis.keys("sfc:queue:item:*")
                item_ids = [k.split(":")[-1] for k in item_keys]

            registros = []
            for item_id in item_ids:
                raw_item = await self.redis.get(f"sfc:queue:item:{item_id}")
                if raw_item:
                    data = json.loads(raw_item)
                    registros.append(ColaItemRedis(data))

            registros.sort(key=lambda x: x.created_at or "", reverse=True)
            return registros
        except Exception as e:
            logger.error(f"Error consultando registros encolados en Redis: {e}")
            return []

    async def purgar_registros_antiguos(self, dias_retencion: int = 7) -> int:
        """Elimina registros en estado 'EXITOSO' con más de N días de antigüedad."""
        if not self.redis:
            return 0

        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        limite_dt = now_bogota - timedelta(days=dias_retencion)

        try:
            item_ids = await self.redis.smembers("sfc:queue:status:EXITOSO")
            purgados = 0

            for item_id in item_ids:
                item_key = f"sfc:queue:item:{item_id}"
                raw_item = await self.redis.get(item_key)
                if not raw_item:
                    await self.redis.srem("sfc:queue:status:EXITOSO", str(item_id))
                    continue

                data = json.loads(raw_item)
                updated_dt = datetime.fromisoformat(data["updated_at"])

                if updated_dt <= limite_dt:
                    await self.redis.delete(item_key)
                    await self.redis.srem("sfc:queue:status:EXITOSO", str(item_id))
                    await self.redis.zrem("sfc:queue:created_zset", str(item_id))
                    purgados += 1

            if purgados > 0:
                logger.info(f"🧹 [Cola Redis] Purga completada: {purgados} registros antiguos eliminados.")

            return purgados
        except Exception as e:
            logger.error(f"Error realizando purga en Redis: {e}")
            return 0