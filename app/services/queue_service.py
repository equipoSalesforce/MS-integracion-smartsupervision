# app/services/queue_service.py
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.email_service import EmailAlertService
from app.core.middleware import get_correlation_id

logger = logging.getLogger(__name__)

QUEUE_PREFIX = "{sfc:queue}"

EXTEND_LEASE_LUA_SCRIPT = """
local claim_key = KEYS[1]
local worker_id = ARGV[1]
local lease_px = tonumber(ARGV[2])

local current_owner = redis.call("GET", claim_key)
if current_owner == worker_id then
    redis.call("PEXPIRE", claim_key, lease_px)
    return cjson.encode({extended = true})
else
    return cjson.encode({extended = false, reason = "owner_mismatch_or_expired"})
end
"""

ENQUEUE_LUA_SCRIPT = """
local index_key = KEYS[1]
local pending_set_key = KEYS[2]
local pending_zset_key = KEYS[3]
local created_zset_key = KEYS[4]
local counter_key = KEYS[5]

local smart_code = ARGV[1]
local tipo_operacion = ARGV[2]
local payload_json_raw = ARGV[3]
local error_inicial = ARGV[4]
local max_intentos = tonumber(ARGV[5])
local now_iso = ARGV[6]
local proximo_reintento_iso = ARGV[7]
local now_ts = tonumber(ARGV[8])
local proximo_reintento_ts = tonumber(ARGV[9])
local correlation_id = ARGV[10]

local existing_id = redis.call("GET", index_key)
local pendientes_count = redis.call("SCARD", pending_set_key)

if existing_id then
    local is_pending = redis.call("SISMEMBER", pending_set_key, existing_id)
    if is_pending == 1 then
        -- 🟢 FIX: Se corrige {sfc_queue} -> {sfc:queue}
        local item_key = "{sfc:queue}:item:" .. existing_id
        local raw_item = redis.call("GET", item_key)
        if raw_item then
            local data = cjson.decode(raw_item)
            data["payload_json"] = cjson.decode(payload_json_raw)
            data["ultimo_error"] = error_inicial
            data["updated_at"] = now_iso
            data["proximo_reintento_at"] = proximo_reintento_iso
            data["correlation_id"] = correlation_id
            data["es_duplicado"] = true

            redis.call("SET", item_key, cjson.encode(data))
            redis.call("ZADD", pending_zset_key, proximo_reintento_ts, existing_id)

            return cjson.encode({
                is_new = false,
                data = data,
                pendientes_previos = pendientes_count
            })
        end
    end
end

local item_id = tostring(redis.call("INCR", counter_key))
-- 🟢 FIX: Se corrige {sfc_queue} -> {sfc:queue}
local item_key = "{sfc:queue}:item:" .. item_id

local item_data = {
    id = tonumber(item_id),
    smart_code = smart_code,
    tipo_operacion = tipo_operacion,
    payload_json = cjson.decode(payload_json_raw),
    estado = "PENDIENTE",
    intentos = 1,
    max_intentos = max_intentos,
    ultimo_error = error_inicial,
    proximo_reintento_at = proximo_reintento_iso,
    created_at = now_iso,
    updated_at = now_iso,
    correlation_id = correlation_id,
    es_duplicado = false
}

redis.call("SET", item_key, cjson.encode(item_data))
redis.call("SADD", pending_set_key, item_id)
redis.call("ZADD", pending_zset_key, proximo_reintento_ts, item_id)
redis.call("ZADD", created_zset_key, now_ts, item_id)
redis.call("SET", index_key, item_id)

return cjson.encode({
    is_new = true,
    data = item_data,
    pendientes_previos = pendientes_count
})
"""

CLAIM_ITEM_LUA_SCRIPT = """
local pending_set_key = KEYS[1]
local claim_key = KEYS[2]

local item_id = ARGV[1]
local worker_id = ARGV[2]
local lease_px = tonumber(ARGV[3])

local is_pending = redis.call("SISMEMBER", pending_set_key, item_id)
if is_pending == 0 then
    return cjson.encode({claimed = false, reason = "not_pending"})
end

local set_res = redis.call("SET", claim_key, worker_id, "NX", "PX", lease_px)
if not set_res then
    return cjson.encode({claimed = false, reason = "already_claimed"})
end

return cjson.encode({claimed = true})
"""


class ColaItemRedis:
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
        self.es_duplicado = bool(data.get("es_duplicado", False))

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
            "es_duplicado": self.es_duplicado
        }

    def to_summary_dict(self) -> dict:
        return {
            "smart_code": self.smart_code,
            "status": self.estado,
            "attempts": self.intentos,
            "max_attempts": self.max_intentos,
            "last_error_code": self.ultimo_error or "N/A",
            "proximo_reintento_at": self.proximo_reintento_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }


class QueueService:
    def __init__(self, redis_client=None):
        self.redis = redis_client

    async def contar_pendientes(self) -> int:
        """🟢 FIX: Usa QUEUE_PREFIX para coincidir con la llave con Hash Tag de Redis."""
        if not self.redis:
            return 0
        try:
            return await self.redis.scard(f"{QUEUE_PREFIX}:status:PENDIENTE")
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
        if not self.redis:
            logger.error("❌ [Cola Redis] Cliente de Redis no inicializado.")
            raise RuntimeError("Cliente de Redis no disponible.")

        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        proximo_reintento = now_bogota + timedelta(minutes=settings.QUEUE_RETRY_INTERVAL_MINUTES)

        keys = [
            f"{QUEUE_PREFIX}:index:{smart_code}",
            f"{QUEUE_PREFIX}:status:PENDIENTE",
            f"{QUEUE_PREFIX}:pending_zset",
            f"{QUEUE_PREFIX}:created_zset",
            f"{QUEUE_PREFIX}:counter"
        ]

        args = [
            smart_code,
            tipo_operacion,
            json.dumps(payload_json, ensure_ascii=False),
            error_inicial or "",
            str(settings.QUEUE_MAX_RETRIES),
            now_bogota.isoformat(),
            proximo_reintento.isoformat(),
            str(now_bogota.timestamp()),
            str(proximo_reintento.timestamp()),
            get_correlation_id() or "N/A"
        ]

        try:
            raw_result = await self.redis.eval(ENQUEUE_LUA_SCRIPT, len(keys), *keys, *args)
            result = json.loads(raw_result)

            item_data = result["data"]
            is_new = result["is_new"]
            pendientes_previos = result["pendientes_previos"]
            item_obj = ColaItemRedis(item_data)

            if not is_new:
                logger.info(
                    f"🔄 [Cola Redis] El caso {smart_code} ya se encontraba encolado (ID: {item_obj.id}). "
                    f"Se actualizó su payload y tiempo de reintento sin crear registros duplicados."
                )
                return item_obj

            logger.warning(f"📦 [Cola Redis] Caso {smart_code} encolado para reintento automático. Registro ID: {item_obj.id}")

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

        except Exception as e:
            logger.error(f"❌ [Cola Redis] Error ejecutando Lua Script de encolado para {smart_code}: {e}")
            raise

    async def reclamar_item_para_procesamiento(
        self, 
        registro_id: int, 
        worker_id: str, 
        lease_segundos: int = 60
    ) -> bool:
        if not self.redis:
            return False

        keys = [
            f"{QUEUE_PREFIX}:status:PENDIENTE",
            f"{QUEUE_PREFIX}:claim:{registro_id}"
        ]
        args = [
            str(registro_id),
            worker_id,
            str(lease_segundos * 1000)
        ]

        try:
            raw_res = await self.redis.eval(CLAIM_ITEM_LUA_SCRIPT, len(keys), *keys, *args)
            res = json.loads(raw_res)
            return res.get("claimed", False)
        except Exception as e:
            logger.error(f"Error al reclamar ítem {registro_id} en Redis: {e}")
            return False

    async def obtener_casos_vencidos_sla(self, horas_limite: int = 12) -> List[Dict[str, Any]]:
        if not self.redis:
            return []

        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        limite_ts = (now_bogota - timedelta(hours=horas_limite)).timestamp()

        try:
            item_ids = await self.redis.zrangebyscore(f"{QUEUE_PREFIX}:created_zset", "-inf", limite_ts)
            casos_vencidos = []

            for item_id in item_ids:
                is_pending = await self.redis.sismember(f"{QUEUE_PREFIX}:status:PENDIENTE", str(item_id))
                if not is_pending:
                    continue

                raw_item = await self.redis.get(f"{QUEUE_PREFIX}:item:{item_id}")
                if not raw_item:
                    continue

                data = json.loads(raw_item, strict=False)
                created_dt = datetime.fromisoformat(data["created_at"])
                horas_en_cola = (now_bogota - created_dt).total_seconds() / 3600.0

                casos_vencidos.append({
                    "smart_code": data["smart_code"],
                    "correlation_id": data.get("correlation_id", "N/A"),
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
        if not self.redis:
            return []

        now_ts = datetime.now(ZoneInfo("America/Bogota")).timestamp()
        try:
            item_ids = await self.redis.zrangebyscore(f"{QUEUE_PREFIX}:pending_zset", "-inf", now_ts)
            pendientes = []

            for item_id in item_ids:
                is_pending = await self.redis.sismember(f"{QUEUE_PREFIX}:status:PENDIENTE", str(item_id))
                if not is_pending:
                    continue

                raw_item = await self.redis.get(f"{QUEUE_PREFIX}:item:{item_id}")
                if not raw_item:
                    continue

                data = json.loads(raw_item, strict=False)
                if data.get("intentos", 0) < data.get("max_intentos", settings.QUEUE_MAX_RETRIES):
                    pendientes.append(ColaItemRedis(data))

            return pendientes
        except Exception as e:
            logger.error(f"Error obteniendo pendientes para reintento en Redis: {e}")
            return []

    async def marcar_exitoso(self, registro_id: int):
        if not self.redis:
            return

        item_key = f"{QUEUE_PREFIX}:item:{registro_id}"
        claim_key = f"{QUEUE_PREFIX}:claim:{registro_id}"
        try:
            raw_item = await self.redis.get(item_key)
            if not raw_item:
                return

            data = json.loads(raw_item, strict=False)
            data["estado"] = "EXITOSO"
            data["updated_at"] = datetime.now(ZoneInfo("America/Bogota")).isoformat()
            smart_code = data.get("smart_code")

            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set(item_key, json.dumps(data, ensure_ascii=False))
                pipe.srem(f"{QUEUE_PREFIX}:status:PENDIENTE", str(registro_id))
                pipe.sadd(f"{QUEUE_PREFIX}:status:EXITOSO", str(registro_id))
                pipe.zrem(f"{QUEUE_PREFIX}:pending_zset", str(registro_id))
                pipe.delete(claim_key)
                if smart_code:
                    pipe.delete(f"{QUEUE_PREFIX}:index:{smart_code}")
                await pipe.execute()

        except Exception as e:
            logger.error(f"Error marcando exitoso registro {registro_id} en Redis: {e}")

    async def registrar_fallo(self, registro_id: int, error_msg: str):
        if not self.redis:
            return

        item_key = f"{QUEUE_PREFIX}:item:{registro_id}"
        claim_key = f"{QUEUE_PREFIX}:claim:{registro_id}"
        try:
            raw_item = await self.redis.get(item_key)
            if not raw_item:
                return

            data = json.loads(raw_item, strict=False)
            data["intentos"] += 1
            data["ultimo_error"] = error_msg
            now_bogota = datetime.now(ZoneInfo("America/Bogota"))
            smart_code = data.get("smart_code")
            
            es_definitivo = data["intentos"] >= data.get("max_intentos", settings.QUEUE_MAX_RETRIES)

            if es_definitivo:
                data["estado"] = "FALLIDO_DEFINITIVO"
                logger.error(f"❌ [Cola Redis] Caso {smart_code} alcanzó el límite máximo de {data['max_intentos']} reintentos.")
                await EmailAlertService.notificar_caso_fallido_definitivo(
                    smart_code=smart_code,
                    total_intentos=data["intentos"],
                    ultimo_error=error_msg,
                    correlation_id=data.get("correlation_id")
                )
                # 🟢 FIX: Invalida en IdempotencyStore para permitir re-envíos desde Salesforce CRM
                from app.services.idempotency_service import IdempotencyService
                idempotency_service = IdempotencyService(self.redis)
                payload_json = data.get("payload_json", {})
                if smart_code and payload_json:
                    await idempotency_service.liberar_por_fallo_definitivo(smart_code=smart_code, payload_dict=payload_json)
            else:
                espera_minutos = settings.QUEUE_RETRY_INTERVAL_MINUTES * data["intentos"]
                proximo_at = now_bogota + timedelta(minutes=espera_minutos)
                data["proximo_reintento_at"] = proximo_at.isoformat()

            data["updated_at"] = now_bogota.isoformat()

            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set(item_key, json.dumps(data, ensure_ascii=False))
                pipe.delete(claim_key)
                
                if es_definitivo:
                    pipe.srem(f"{QUEUE_PREFIX}:status:PENDIENTE", str(registro_id))
                    pipe.sadd(f"{QUEUE_PREFIX}:status:FALLIDO_DEFINITIVO", str(registro_id))
                    pipe.zrem(f"{QUEUE_PREFIX}:pending_zset", str(registro_id))
                    if smart_code:
                        pipe.delete(f"{QUEUE_PREFIX}:index:{smart_code}")
                else:
                    pipe.zadd(f"{QUEUE_PREFIX}:pending_zset", {str(registro_id): proximo_at.timestamp()})
                
                await pipe.execute()

        except Exception as e:
            logger.error(f"Error registrando fallo para registro {registro_id} en Redis: {e}")

    async def obtener_todos_los_encolados(self, estado: Optional[str] = None) -> List[ColaItemRedis]:
        if not self.redis:
            return []

        try:
            if estado:
                set_key = f"{QUEUE_PREFIX}:status:{estado.upper()}"
                if hasattr(self.redis, "sscan_iter"):
                    item_ids = [
                        m if isinstance(m, str) else m.decode("utf-8")
                        async for m in self.redis.sscan_iter(set_key, count=100)
                    ]
                else:
                    raw_members = await self.redis.smembers(set_key)
                    item_ids = [m if isinstance(m, str) else m.decode("utf-8") for m in raw_members]
            else:
                if hasattr(self.redis, "scan_iter"):
                    item_keys = [
                        k if isinstance(k, str) else k.decode("utf-8")
                        async for k in self.redis.scan_iter(match=f"{QUEUE_PREFIX}:item:*", count=100)
                    ]
                else:
                    raw_keys = await self.redis.keys(f"{QUEUE_PREFIX}:item:*")
                    item_keys = [k if isinstance(k, str) else k.decode("utf-8") for k in raw_keys]

                item_ids = [k.split(":")[-1] for k in item_keys]

            registros = []
            for item_id in item_ids:
                raw_item = await self.redis.get(f"{QUEUE_PREFIX}:item:{item_id}")
                if raw_item:
                    data = json.loads(raw_item, strict=False)
                    registros.append(ColaItemRedis(data))

            registros.sort(key=lambda x: x.created_at or "", reverse=True)
            return registros
        except Exception as e:
            logger.error(f"Error consultando registros encolados en Redis: {e}")
            return []

    async def purgar_registros_antiguos(
        self, 
        dias_retencion: int = settings.QUEUE_RETENTION_DAYS, 
        dias_retencion_dlq: int = getattr(settings, "QUEUE_RETENTION_DAYS_DLQ", 30)
    ) -> int:
        if not self.redis:
            return 0

        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        limite_exitoso = now_bogota - timedelta(days=dias_retencion)
        limite_dlq = now_bogota - timedelta(days=dias_retencion_dlq)

        total_purgados = 0

        estados_a_evaluar = [
            (f"{QUEUE_PREFIX}:status:EXITOSO", limite_exitoso),
            (f"{QUEUE_PREFIX}:status:FALLIDO_DEFINITIVO", limite_dlq)
        ]

        try:
            for set_key, limite_dt in estados_a_evaluar:
                if hasattr(self.redis, "sscan_iter"):
                    item_ids = [
                        m if isinstance(m, str) else m.decode("utf-8")
                        async for m in self.redis.sscan_iter(set_key, count=100)
                    ]
                else:
                    raw_members = await self.redis.smembers(set_key)
                    item_ids = [m if isinstance(m, str) else m.decode("utf-8") for m in raw_members]

                a_eliminar = []
                inconsistentes = []

                for item_id in item_ids:
                    item_key = f"{QUEUE_PREFIX}:item:{item_id}"
                    raw_item = await self.redis.get(item_key)
                    if not raw_item:
                        inconsistentes.append(str(item_id))
                        continue

                    data = json.loads(raw_item, strict=False)
                    updated_dt = datetime.fromisoformat(data["updated_at"])

                    if updated_dt <= limite_dt:
                        a_eliminar.append(str(item_id))

                if a_eliminar or inconsistentes:
                    async with self.redis.pipeline(transaction=True) as pipe:
                        for item_id in inconsistentes:
                            pipe.srem(set_key, item_id)

                        for item_id in a_eliminar:
                            pipe.delete(f"{QUEUE_PREFIX}:item:{item_id}")
                            pipe.srem(set_key, item_id)
                            pipe.zrem(f"{QUEUE_PREFIX}:created_zset", item_id)
                            total_purgados += 1

                        await pipe.execute()

            if total_purgados > 0:
                logger.info(f"🧹 [Cola Redis] Purga completada: {total_purgados} registros antiguos (Exitosos/DLQ) eliminados.")

            return total_purgados
        except Exception as e:
            logger.error(f"Error realizando purga en Redis: {e}")
            return 0

    async def diferir_pendientes_por_caida_sfc(self, registro_ids: List[int], minutos_delay: Optional[int] = None) -> int:
        if not self.redis or not registro_ids:
            return 0

        delay_min = minutos_delay or settings.QUEUE_RETRY_INTERVAL_MINUTES
        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        proximo_at = now_bogota + timedelta(minutes=delay_min)
        proximo_ts = proximo_at.timestamp()

        modificados = 0
        for registro_id in registro_ids:
            item_key = f"{QUEUE_PREFIX}:item:{registro_id}"
            claim_key = f"{QUEUE_PREFIX}:claim:{registro_id}"
            try:
                raw_item = await self.redis.get(item_key)
                if not raw_item:
                    continue

                data = json.loads(raw_item, strict=False)
                data["proximo_reintento_at"] = proximo_at.isoformat()
                data["updated_at"] = now_bogota.isoformat()
                data["ultimo_error"] = "Reintento pospuesto automáticamente por caída de plataforma SFC."

                async with self.redis.pipeline(transaction=True) as pipe:
                    pipe.set(item_key, json.dumps(data, ensure_ascii=False))
                    pipe.zadd(f"{QUEUE_PREFIX}:pending_zset", {str(registro_id): proximo_ts})
                    pipe.delete(claim_key)
                    await pipe.execute()

                modificados += 1
            except Exception as e:
                logger.error(f"Error difiriendo registro {registro_id} por caída SFC: {e}")

        logger.warning(f"🛑 [Cola Redis] Se diferió la ejecución de {modificados} casos por {delay_min} min sin consumir intentos.")
        return modificados
    
    async def extender_lease_item(
        self, 
        registro_id: int, 
        worker_id: str, 
        lease_segundos: int = 60
    ) -> bool:
        if not self.redis:
            return False

        keys = [f"{QUEUE_PREFIX}:claim:{registro_id}"]
        args = [worker_id, str(lease_segundos * 1000)]

        try:
            raw_res = await self.redis.eval(EXTEND_LEASE_LUA_SCRIPT, len(keys), *keys, *args)
            res = json.loads(raw_res)
            return res.get("extended", False)
        except Exception as e:
            logger.error(f"Error al extender lease del ítem {registro_id} en Redis: {e}")
            return False