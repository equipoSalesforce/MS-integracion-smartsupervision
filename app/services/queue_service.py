# app/services/queue_service.py
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.constants import SmartStatus
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
local estado_pendiente = ARGV[11]

local existing_id = redis.call("GET", index_key)
local pendientes_count = redis.call("SCARD", pending_set_key)

if existing_id then
    local is_pending = redis.call("SISMEMBER", pending_set_key, existing_id)
    if is_pending == 1 then
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
            -- 🟢 FIX P0-04: cada sobrescritura de un item pendiente sube su versión, para
            -- que un worker que ya estaba procesando la versión anterior pueda detectar
            -- en MARK_SUCCESS que el contenido cambió bajo sus pies y NO lo marque COMPLETED.
            data["version"] = (tonumber(data["version"]) or 1) + 1
            -- 🟢 FIX observabilidad (auditoría adversarial v9, sección 6): el contenido
            -- vigente es nuevo y todavía nadie lo reclamó -- si el item anterior estaba en
            -- PROCESSING (un worker viejo procesando la versión que se acaba de sobrescribir),
            -- ese estado no debe heredarse al contenido nuevo.
            data["estado"] = estado_pendiente

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
local item_key = "{sfc:queue}:item:" .. item_id

local item_data = {
    id = tonumber(item_id),
    smart_code = smart_code,
    tipo_operacion = tipo_operacion,
    payload_json = cjson.decode(payload_json_raw),
    estado = estado_pendiente,
    sfc_completado = false,
    sfc_response = nil,
    intentos = 1,
    max_intentos = max_intentos,
    ultimo_error = error_inicial,
    proximo_reintento_at = proximo_reintento_iso,
    created_at = now_iso,
    updated_at = now_iso,
    correlation_id = correlation_id,
    es_duplicado = false,
    version = 1
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
local item_key = KEYS[3]

local item_id = ARGV[1]
local worker_id = ARGV[2]
local lease_px = tonumber(ARGV[3])
local estado_procesando = ARGV[4]

local is_pending = redis.call("SISMEMBER", pending_set_key, item_id)
if is_pending == 0 then
    return cjson.encode({claimed = false, reason = "not_pending"})
end

local set_res = redis.call("SET", claim_key, worker_id, "NX", "PX", lease_px)
if not set_res then
    return cjson.encode({claimed = false, reason = "already_claimed"})
end

-- 🟢 FIX P0-04: devolver el item TAL COMO ESTÁ en Redis en el mismo paso atómico que el
-- claim, en vez de que el caller reutilice una copia leída antes de reclamar (ventana en
-- la que el payload pudo haber sido sobrescrito por un evento más nuevo del mismo caso).
local raw_item = redis.call("GET", item_key)
if not raw_item then
    redis.call("DEL", claim_key)
    return cjson.encode({claimed = false, reason = "item_not_found"})
end

-- 🟢 FIX observabilidad (auditoría adversarial v9, sección 6): reflejar en el propio
-- item que está siendo procesado -- antes el campo `estado` se quedaba en PENDING todo
-- el tiempo que duraba el procesamiento, indistinguible (desde /queue) de un item que
-- ni siquiera había sido reclamado. SmartStatus.PROCESSING existía declarado pero
-- nunca se usaba.
local data = cjson.decode(raw_item)
data["estado"] = estado_procesando
redis.call("SET", item_key, cjson.encode(data))

return cjson.encode({claimed = true, item = data})
"""

MARK_SUCCESS_LUA_SCRIPT = """
local item_key = KEYS[1]
local pending_set_key = KEYS[2]
local completed_set_key = KEYS[3]
local pending_zset_key = KEYS[4]
local claim_key = KEYS[5]

local item_id = ARGV[1]
local now_iso = ARGV[2]
local estado_completed = ARGV[3]
local worker_id = ARGV[4]
local expected_version = tonumber(ARGV[5])

-- 🟢 FIX P0-05: sólo el worker que sigue siendo dueño del lease puede completar el item.
-- Antes se borraba el claim incondicionalmente, permitiendo que un worker cuyo lease ya
-- expiró (y que por lo tanto otro worker ya reclamó) completara igual y le borrara el
-- claim activo al nuevo dueño.
local current_owner = redis.call("GET", claim_key)
if current_owner ~= worker_id then
    return cjson.encode({success = false, reason = "not_owner"})
end

local raw_item = redis.call("GET", item_key)
if not raw_item then
    redis.call("DEL", claim_key)
    return cjson.encode({success = false, reason = "item_not_found"})
end

local data = cjson.decode(raw_item)

-- 🟢 FIX P0-04: si el item fue sobrescrito por un evento más nuevo del mismo smart_code
-- mientras este worker lo procesaba (version distinta a la que reclamó), NO completar.
-- El contenido que se envió a SFC ya fue transmitido correctamente, pero el registro
-- actual en Redis ya no representa ese envío: se libera el claim y se deja el item
-- pendiente (con el próximo reintento que el propio ENQUEUE ya programó) para que el
-- contenido vigente se transmita en su turno, en vez de marcarlo COMPLETED sin haberlo
-- enviado nunca.
if tonumber(data["version"] or 1) ~= expected_version then
    redis.call("DEL", claim_key)
    return cjson.encode({success = false, reason = "version_mismatch"})
end

data["estado"] = estado_completed
data["updated_at"] = now_iso

redis.call("SET", item_key, cjson.encode(data))
redis.call("SREM", pending_set_key, item_id)
redis.call("SADD", completed_set_key, item_id)
redis.call("ZREM", pending_zset_key, item_id)
redis.call("DEL", claim_key)

if data["smart_code"] and data["smart_code"] ~= "" then
    local index_key = "{sfc:queue}:index:" .. data["smart_code"]
    redis.call("DEL", index_key)
end

return cjson.encode({success = true})
"""

MARK_SFC_DONE_LUA_SCRIPT = """
local item_key = KEYS[1]
local claim_key = KEYS[2]
local idempotency_key = KEYS[3]

local worker_id = ARGV[1]
local expected_version = tonumber(ARGV[2])
local now_iso = ARGV[3]
local estado_sfc_done = ARGV[4]
local has_sfc_response = ARGV[5]
local sfc_response_json = ARGV[6]
local idempotency_record_json = ARGV[7]
local idempotency_ttl_ms = tonumber(ARGV[8])

-- 🟢 FIX P0-01 (auditoría adversarial v9): sólo el worker dueño del lease puede
-- persistir SFC_DONE. Antes cualquier worker (incluso uno "viejo" cuyo item ya fue
-- sobrescrito por un evento más nuevo del mismo caso) podía marcar el item vigente
-- como completado usando SU PROPIA respuesta de SFC, haciendo que scheduler.py
-- saltara el envío real del evento nuevo en el siguiente ciclo.
local current_owner = redis.call("GET", claim_key)
if current_owner ~= worker_id then
    return cjson.encode({success = false, reason = "not_owner"})
end

local raw_item = redis.call("GET", item_key)
if not raw_item then
    redis.call("DEL", claim_key)
    return cjson.encode({success = false, reason = "item_not_found"})
end

local data = cjson.decode(raw_item)

-- Mismo criterio que MARK_SUCCESS: si el item fue sobrescrito por un evento más
-- nuevo del mismo smart_code mientras se procesaba, no persistir SFC_DONE sobre el
-- contenido vigente -- la respuesta de SFC pertenece a la versión anterior.
if tonumber(data["version"] or 1) ~= expected_version then
    redis.call("DEL", claim_key)
    return cjson.encode({success = false, reason = "version_mismatch"})
end

data["sfc_completado"] = true
data["estado"] = estado_sfc_done
if has_sfc_response == "1" then
    data["sfc_response"] = cjson.decode(sfc_response_json)
end
data["updated_at"] = now_iso

redis.call("SET", item_key, cjson.encode(data))

-- 🟢 Nivel 1 (auditoría adversarial v10, P0-02): el registro de idempotencia
-- COMPLETED se escribe en la MISMA ejecución atómica que SFC_DONE, después de
-- superar las validaciones de ownership/versión de arriba -- antes eran dos
-- llamadas Redis secuenciales e independientes (una desde IdempotencyService, otra
-- desde QueueService); si la primera tenía éxito y la segunda fallaba (o el propio
-- worker perdía ownership entre medio), quedaba COMPLETED en idempotencia pero SIN
-- SFC_DONE en la cola -- el próximo ciclo no consultaba el Idempotency Store antes
-- de reenviar, y la SFC se volvía a llamar. Con una sola escritura atómica, o
-- quedan las dos persistidas juntas, o ninguna.
redis.call("SET", idempotency_key, idempotency_record_json, "PX", idempotency_ttl_ms)

return cjson.encode({success = true})
"""

REGISTRAR_FALLO_LUA_SCRIPT = """
local item_key = KEYS[1]
local claim_key = KEYS[2]
local pending_set_key = KEYS[3]
local failed_set_key = KEYS[4]
local pending_zset_key = KEYS[5]

local worker_id = ARGV[1]
local expected_version = tonumber(ARGV[2])
local error_msg = ARGV[3]
local now_iso = ARGV[4]
local es_definitivo = ARGV[5]
local estado_failed_final = ARGV[6]
local proximo_reintento_iso = ARGV[7]
local proximo_reintento_ts = ARGV[8]
local item_id = ARGV[9]
local estado_pendiente = ARGV[10]
local consumir_intento = ARGV[11]

-- 🟢 FIX P0-02 (auditoría adversarial v9): sólo el worker dueño del lease puede
-- registrar un fallo. Antes cualquier worker (incluso uno "viejo" cuyo item ya fue
-- sobrescrito por un evento más nuevo del mismo caso) podía aplicar SU error,
-- incrementar los intentos y borrar el claim del nuevo dueño legítimo.
local current_owner = redis.call("GET", claim_key)
if current_owner ~= worker_id then
    return cjson.encode({success = false, reason = "not_owner"})
end

local raw_item = redis.call("GET", item_key)
if not raw_item then
    redis.call("DEL", claim_key)
    return cjson.encode({success = false, reason = "item_not_found"})
end

local data = cjson.decode(raw_item)

if tonumber(data["version"] or 1) ~= expected_version then
    redis.call("DEL", claim_key)
    return cjson.encode({success = false, reason = "version_mismatch"})
end

if consumir_intento == "1" then
    data["intentos"] = (tonumber(data["intentos"]) or 0) + 1
end
data["ultimo_error"] = error_msg
data["updated_at"] = now_iso

if es_definitivo == "1" then
    data["estado"] = estado_failed_final
else
    -- 🟢 FIX observabilidad (auditoría adversarial v9, sección 6): el claim se libera
    -- (abajo) y el item vuelve a esperar su próximo turno de reintento -- ya no está
    -- "en proceso", así que el estado debe reflejarlo (antes se quedaba en PROCESSING,
    -- heredado del claim, hasta el siguiente ciclo que lo reclamara de nuevo).
    data["estado"] = estado_pendiente
    data["proximo_reintento_at"] = proximo_reintento_iso
end

redis.call("SET", item_key, cjson.encode(data))
redis.call("DEL", claim_key)

if es_definitivo == "1" then
    redis.call("SREM", pending_set_key, item_id)
    redis.call("SADD", failed_set_key, item_id)
    redis.call("ZREM", pending_zset_key, item_id)
    if data["smart_code"] and data["smart_code"] ~= "" then
        redis.call("DEL", "{sfc:queue}:index:" .. data["smart_code"])
    end
else
    redis.call("ZADD", pending_zset_key, proximo_reintento_ts, item_id)
end

return cjson.encode({success = true})
"""


class ColaItemRedis:
    def __init__(self, data: dict):
        self.id = int(data.get("id")) if data.get("id") else None
        self.smart_code = str(data.get("smart_code", ""))
        self.tipo_operacion = str(data.get("tipo_operacion", "AUTO"))
        self.payload_json = data.get("payload_json", {})
        self.estado = str(data.get("estado", SmartStatus.PENDING.value))
        self.sfc_completado = bool(data.get("sfc_completado", False))
        self.sfc_response = data.get("sfc_response")
        self.intentos = int(data.get("intentos", 0))
        self.max_intentos = int(data.get("max_intentos", settings.QUEUE_MAX_RETRIES))
        self.ultimo_error = data.get("ultimo_error")
        self.proximo_reintento_at = data.get("proximo_reintento_at")
        self.created_at = data.get("created_at")
        self.updated_at = data.get("updated_at")
        # 🟢 FIX HALLAZGO 47: Preservar correlation_id en el modelo duradero de Python
        self.correlation_id = str(data.get("correlation_id", "N/A"))
        self.es_duplicado = bool(data.get("es_duplicado", False))
        # 🟢 FIX P0-04: versión del payload, usada para detectar sobrescrituras concurrentes
        self.version = int(data.get("version", 1))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "smart_code": self.smart_code,
            "tipo_operacion": self.tipo_operacion,
            "payload_json": self.payload_json,
            "estado": self.estado,
            "sfc_completado": self.sfc_completado,
            "sfc_response": self.sfc_response,
            "intentos": self.intentos,
            "max_intentos": self.max_intentos,
            "ultimo_error": self.ultimo_error,
            "proximo_reintento_at": self.proximo_reintento_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "correlation_id": self.correlation_id,  # 🟢 FIX HALLAZGO 47
            "es_duplicado": self.es_duplicado,
            "version": self.version  # 🟢 FIX P0-04
        }

    def to_summary_dict(self) -> dict:
        return {
            "smart_code": self.smart_code,
            "status": self.estado,
            "sfc_completado": self.sfc_completado,
            "attempts": self.intentos,
            "max_attempts": self.max_intentos,
            "last_error_code": self.ultimo_error or "N/A",
            "proximo_reintento_at": self.proximo_reintento_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "correlation_id": self.correlation_id  # 🟢 FIX HALLAZGO 47
        }


class QueueService:
    def __init__(self, redis_client=None):
        self.redis = redis_client

    def _crear_pipeline_compatible(self):
        """
        🟢 FIX P1-03: antes se usaba `transaction=False` (pipeline no atómico, sin
        garantía todo-o-nada) en modo cluster, dejando expuestas las transiciones
        multi-clave a quedar parcialmente aplicadas ante una interrupción a mitad de
        camino. Todas las claves del dominio de cola comparten el mismo hash-tag
        "{sfc:queue}" como prefijo (ver QUEUE_PREFIX) — Redis Cluster enruta por hash
        tag, así que TODAS caen siempre en el mismo slot. Eso hace seguro usar
        `transaction=True` (MULTI/EXEC real) también en cluster, sin riesgo de
        CROSSSLOT, y con la misma atomicidad que en modo standalone.
        """
        if not self.redis:
            return None
        return self.redis.pipeline(transaction=True)

    async def contar_pendientes(self) -> int:
        """
        🟢 FIX P1-15: ya no se traga la excepción devolviendo 0. Un fallo real de Redis
        aquí era indistinguible de "cola vacía" — y como este método decide si se envía
        la notificación de recuperación total (`casos_despachados_exito > 0 and
        totales_restantes == 0` en scheduler.py), un fallo silencioso podía disparar un
        falso aviso de "recuperación completa" cuando en realidad no se pudo verificar.
        """
        if not self.redis:
            return 0
        return await self.redis.scard(f"{QUEUE_PREFIX}:status:{SmartStatus.PENDING.value}")

    async def obtener_edad_item_mas_antiguo_pendiente(self) -> Optional[float]:
        """
        Observabilidad: antigüedad (segundos) del item PENDING más antiguo en la
        cola, usada para el metric oldest_pending_age_seconds. Recorre created_zset
        desde el más antiguo, saltando ids que ya no estén en estado PENDING
        (p.ej. completados/fallidos definitivos aún no purgados del zset) — mismo
        patrón defensivo que ya usa obtener_casos_vencidos_sla. Devuelve None si la
        cola está vacía o si no se pudo determinar (nunca lanza).
        """
        if not self.redis:
            return None
        try:
            candidatos = await self.redis.zrangebyscore(
                f"{QUEUE_PREFIX}:created_zset", "-inf", "+inf",
                start=0, num=20, withscores=True
            )
            if not candidatos:
                return None

            now_ts = datetime.now(ZoneInfo("America/Bogota")).timestamp()
            for item_id, created_ts in candidatos:
                is_pending = await self.redis.sismember(
                    f"{QUEUE_PREFIX}:status:{SmartStatus.PENDING.value}", str(item_id)
                )
                if is_pending:
                    return max(0.0, now_ts - created_ts)
            return None
        except Exception as e:
            logger.warning(f"No se pudo calcular la antigüedad del item más antiguo pendiente: {e}")
            return None

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
            f"{QUEUE_PREFIX}:status:{SmartStatus.PENDING.value}",
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
            get_correlation_id() or "N/A",
            SmartStatus.PENDING.value
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

    async def marcar_sfc_completado(
        self,
        registro_id: int,
        worker_id: str,
        expected_version: int,
        smart_code: str,
        payload_dict: dict,
        sfc_response: Optional[Dict[str, Any]] = None,
        max_intentos_persistencia: int = 3
    ) -> str:
        """
        🟢 FIX P0-01 (auditoría adversarial v9): transición atómica Lua con ownership +
        versión, mismo patrón que marcar_exitoso. Antes era un GET+mutate+SET
        incondicional sin worker_id/expected_version: un worker "viejo" (cuyo item ya
        fue sobrescrito por un evento más nuevo del mismo caso) podía marcar el item
        vigente como SFC_DONE usando SU PROPIA respuesta de SFC — scheduler.py usa
        item.sfc_completado para saltar el PASO 1 de despacho, así que el evento nuevo
        podía terminar saltándose el envío real a la SFC.

        🟢 Nivel 1 (auditoría adversarial v10, P0-02): smart_code/payload_dict son
        nuevos -- se usan para construir, dentro del MISMO script Lua, la clave y el
        registro COMPLETED del Idempotency Store (ver IdempotencyService.
        construir_clave_completado/construir_registro_completado). Antes el caller
        (scheduler.py) llamaba primero a IdempotencyService.registrar_exito() (una
        escritura Redis independiente) y sólo después a este método: si la primera
        tenía éxito y la segunda fallaba, quedaba COMPLETED en idempotencia pero sin
        SFC_DONE en la cola -- un estado dividido que este método ya no puede producir,
        porque ambas escrituras ahora ocurren atómicamente o no ocurre ninguna. De
        paso, la escritura de idempotencia queda condicionada a pasar la MISMA
        validación de ownership/versión que ya protegía a SFC_DONE -- antes
        registrar_exito() se ejecutaba incondicionalmente, incluso si este método
        iba a rechazar la transición por "not_owner"/"version_mismatch".

        🟢 FIX P0-06 (se conserva): esta sigue siendo la escritura más crítica del
        flujo — si SFC ya recibió y procesó el envío pero esto falla en persistirse
        por una caída real de Redis, el próximo reintento puede reenviar la misma
        operación. El reintento con backoff sólo aplica a fallos de infraestructura;
        "not_owner"/"version_mismatch"/"not_found" son resultados legítimos de la
        validación atómica y se retornan de inmediato sin reintentar.

        Retorna "completed", "not_owner", "version_mismatch" o "not_found". Sólo
        lanza ante un fallo real de Redis/Lua tras agotar los reintentos.
        """
        if not self.redis:
            raise RuntimeError("Cliente de Redis no disponible al intentar persistir SFC_DONE.")

        # Import diferido: evita el ciclo de imports entre queue_service e
        # idempotency_service (mismo patrón ya usado en registrar_fallo, más abajo).
        from app.services.idempotency_service import IdempotencyService

        idempotency_key = IdempotencyService.construir_clave_completado(smart_code, payload_dict)
        idempotency_record = IdempotencyService.construir_registro_completado(
            smart_code, payload_dict, sfc_response
        )

        item_key = f"{QUEUE_PREFIX}:item:{registro_id}"
        claim_key = f"{QUEUE_PREFIX}:claim:{registro_id}"
        keys = [item_key, claim_key, idempotency_key]
        args = [
            worker_id,
            str(expected_version),
            datetime.now(ZoneInfo("America/Bogota")).isoformat(),
            SmartStatus.SFC_DONE.value,
            "1" if sfc_response is not None else "0",
            json.dumps(sfc_response, ensure_ascii=False) if sfc_response is not None else "",
            json.dumps(idempotency_record, ensure_ascii=False),
            str(IdempotencyService.TTL_SECONDS_DEFAULT * 1000)
        ]

        ultimo_error: Optional[Exception] = None

        for intento in range(1, max_intentos_persistencia + 1):
            try:
                raw_res = await self.redis.eval(MARK_SFC_DONE_LUA_SCRIPT, len(keys), *keys, *args)
                res = json.loads(raw_res)

                if not res.get("success"):
                    reason = res.get("reason", "error_desconocido")
                    if reason == "not_owner":
                        logger.warning(
                            f"⚠️ [Cola Redis] Registro {registro_id} ya no pertenece al worker {worker_id} "
                            f"al intentar marcar sfc_completado. No se persiste SFC_DONE desde aquí."
                        )
                        return "not_owner"
                    if reason == "version_mismatch":
                        logger.warning(
                            f"⚠️ [Cola Redis] Registro {registro_id} fue sobrescrito por un evento más nuevo "
                            f"del mismo caso (versión esperada {expected_version}) al intentar marcar "
                            f"sfc_completado. No se persiste la respuesta de SFC sobre el contenido vigente."
                        )
                        return "version_mismatch"
                    if reason == "item_not_found":
                        logger.warning(f"⚠️ [Cola Redis] No se encontró el registro {registro_id} al marcar sfc_completado.")
                        return "not_found"
                    raise RuntimeError(
                        f"Fallo en transición a {SmartStatus.SFC_DONE.value} en Redis para el registro {registro_id}. Razón: {reason}"
                    )

                logger.info(f"📌 [Cola Redis] Ítem {registro_id} actualizado a {SmartStatus.SFC_DONE.value}.")
                return "completed"

            except Exception as e:
                ultimo_error = e
                logger.warning(
                    f"⚠️ [Cola Redis] Intento {intento}/{max_intentos_persistencia} fallido marcando "
                    f"sfc_completado para registro {registro_id}: {e}"
                )
                if intento < max_intentos_persistencia:
                    await asyncio.sleep(0.5 * intento)

        raise RuntimeError(
            f"No fue posible persistir SFC_DONE para el registro {registro_id} tras "
            f"{max_intentos_persistencia} intentos. Último error: {ultimo_error}"
        )

    async def reclamar_item_para_procesamiento(
        self,
        registro_id: int,
        worker_id: str,
        lease_segundos: int = 60
    ) -> Optional[ColaItemRedis]:
        """
        Reclama el item y devuelve, en el MISMO paso atómico, el contenido tal como está
        en Redis en ese instante (🟢 FIX P0-04). Devolver un simple bool obligaba al caller
        a reusar la copia leída durante el listado previo, que pudo haber sido sobrescrita
        por un evento más nuevo del mismo smart_code entre el listado y el claim.
        """
        if not self.redis:
            return None

        keys = [
            f"{QUEUE_PREFIX}:status:{SmartStatus.PENDING.value}",
            f"{QUEUE_PREFIX}:claim:{registro_id}",
            f"{QUEUE_PREFIX}:item:{registro_id}"
        ]
        args = [
            str(registro_id),
            worker_id,
            str(lease_segundos * 1000),
            SmartStatus.PROCESSING.value
        ]

        try:
            raw_res = await self.redis.eval(CLAIM_ITEM_LUA_SCRIPT, len(keys), *keys, *args)
            res = json.loads(raw_res)
            if not res.get("claimed", False):
                return None
            return ColaItemRedis(res["item"])
        except Exception as e:
            logger.error(f"Error al reclamar ítem {registro_id} en Redis: {e}")
            return None

    async def obtener_casos_vencidos_sla(self, horas_limite: int = 12) -> List[Dict[str, Any]]:
        """
        🟢 FIX P1-15: ya no se traga la excepción devolviendo []. El caller
        (scheduler.py) ya envuelve esta llamada en su propio try/except, así que dejar
        propagar el error real permite loguearlo distinto de "no hay casos vencidos".
        """
        if not self.redis:
            return []

        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        limite_ts = (now_bogota - timedelta(hours=horas_limite)).timestamp()

        item_ids = await self.redis.zrangebyscore(f"{QUEUE_PREFIX}:created_zset", "-inf", limite_ts)
        casos_vencidos = []

        for item_id in item_ids:
            is_pending = await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.PENDING.value}", str(item_id))
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

    async def obtener_pendientes_para_reintento(self) -> List[ColaItemRedis]:
        """
        🟢 FIX P1-15: ya no se traga la excepción devolviendo []. Este método determina
        QUÉ procesa el scheduler en cada ciclo — un fallo de Redis silenciado aquí era
        indistinguible de "no hay nada pendiente", pudiendo ocultar una caída real
        durante todo el tiempo que dure. El caller ahora debe manejar la excepción
        explícitamente (ver reintentar_despachos_pendientes_job).
        """
        if not self.redis:
            return []

        now_ts = datetime.now(ZoneInfo("America/Bogota")).timestamp()
        item_ids = await self.redis.zrangebyscore(f"{QUEUE_PREFIX}:pending_zset", "-inf", now_ts)
        pendientes = []

        for item_id in item_ids:
            is_pending = await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.PENDING.value}", str(item_id))
            if not is_pending:
                continue

            raw_item = await self.redis.get(f"{QUEUE_PREFIX}:item:{item_id}")
            if not raw_item:
                continue

            data = json.loads(raw_item, strict=False)
            if data.get("intentos", 0) < data.get("max_intentos", settings.QUEUE_MAX_RETRIES):
                pendientes.append(ColaItemRedis(data))

        return pendientes

    async def marcar_exitoso(self, registro_id: int, worker_id: str, expected_version: int) -> str:
        """
        Retorna el resultado de la transición: "completed", "not_owner",
        "version_mismatch" o "not_found". Sólo lanza excepción ante un fallo real de
        Redis/Lua (conexión, script, etc.) — "not_owner"/"version_mismatch" son carreras
        benignas (🟢 FIX P0-04/P0-05): el envío a SFC sí ocurrió, pero este worker ya no
        tiene autoridad sobre el registro (perdió el lease, o el contenido fue
        sobrescrito por un evento más nuevo del mismo caso) y por eso NO debe completarlo
        ni contarse como fallo de reintentos.
        """
        if not self.redis:
            logger.error("❌ [Cola Redis] Cliente de Redis no inicializado al intentar marcar éxito.")
            raise RuntimeError("Cliente de Redis no disponible.")

        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        keys = [
            f"{QUEUE_PREFIX}:item:{registro_id}",
            f"{QUEUE_PREFIX}:status:{SmartStatus.PENDING.value}",
            f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}",
            f"{QUEUE_PREFIX}:pending_zset",
            f"{QUEUE_PREFIX}:claim:{registro_id}"
        ]
        args = [
            str(registro_id),
            now_bogota.isoformat(),
            SmartStatus.COMPLETED.value,
            worker_id,
            str(expected_version)
        ]

        try:
            raw_res = await self.redis.eval(MARK_SUCCESS_LUA_SCRIPT, len(keys), *keys, *args)
            res = json.loads(raw_res)

            if not res.get("success"):
                reason = res.get("reason", "error_desconocido")
                if reason == "item_not_found":
                    logger.warning(f"⚠️ [Cola Redis] Intentando marcar como exitoso un registro inexistente: {registro_id}")
                    return "not_found"
                if reason == "not_owner":
                    logger.warning(
                        f"⚠️ [Cola Redis] Registro {registro_id} ya no pertenece al worker {worker_id} "
                        f"(lease perdido/reclamado por otro worker). No se completa desde aquí."
                    )
                    return "not_owner"
                if reason == "version_mismatch":
                    logger.warning(
                        f"⚠️ [Cola Redis] Registro {registro_id} fue sobrescrito por un evento más nuevo "
                        f"del mismo caso mientras se procesaba (versión esperada {expected_version}). "
                        f"No se marca COMPLETED; el contenido vigente se reintentará en su turno."
                    )
                    return "version_mismatch"
                raise RuntimeError(
                    f"Fallo en transición de estado a {SmartStatus.COMPLETED.value} en Redis para el registro {registro_id}. Razón: {reason}"
                )

            logger.info(f"✅ [Cola Redis] Ítem {registro_id} actualizado a {SmartStatus.COMPLETED.value} exitosamente.")
            return "completed"

        except Exception as e:
            logger.error(f"❌ [Cola Redis] Excepción al marcar exitoso el registro {registro_id}: {e}")
            raise

    async def registrar_fallo(
        self,
        item: "ColaItemRedis",
        error_msg: str,
        worker_id: str,
        consumir_intento: bool = True
    ) -> str:
        """
        🟢 FIX P0-02 (auditoría adversarial v9): transición atómica Lua con ownership +
        versión, mismo patrón que marcar_exitoso/marcar_sfc_completado. Antes era un
        GET+mutate+SET incondicional: un worker "viejo" (lease expirado, o cuyo item
        fue sobrescrito por un evento más nuevo del mismo caso) podía aplicar SU
        error, incrementar los intentos y borrar el claim del NUEVO dueño legítimo
        del registro — pudiendo empujarlo a FAILED_FINAL/DLQ sin que realmente
        hubiera fallado esas veces.

        Recibe el `item` reclamado (no sólo el id) porque intentos/max_intentos ya
        se conocen de forma confiable: si la validación de versión dentro del Lua
        script acepta la escritura, el estado en Redis es EXACTAMENTE el que este
        worker reclamó (nadie más pudo tocarlo sin subir la versión), así que los
        valores derivados (intentos+1, si es definitivo, próximo reintento) se
        precalculan aquí en Python en vez de reimplementar el formateo de fechas
        ISO/zona horaria dentro de Lua.

        `consumir_intento=False` (ej. fallo del webhook al CRM clasificado como
        caída de infraestructura, ver `_es_falla_infraestructura` en scheduler.py)
        registra el error y reprograma el reintento SIN incrementar `intentos` ni
        poder marcar el registro como definitivo/DLQ en esta llamada -- la SFC ya
        proceso el caso con éxito en ese escenario, así que agotar el límite de
        reintentos por una caída puramente del lado del CRM sería un falso FAILED_FINAL.

        Retorna "failed" (fallo aplicado), "not_owner", "version_mismatch" o
        "not_found". Nunca lanza — un fallo real de Redis se loguea y se retorna
        como "not_found" para no interrumpir el ciclo del scheduler por un item.
        """
        if not self.redis:
            return "not_found"

        registro_id = item.id
        item_key = f"{QUEUE_PREFIX}:item:{registro_id}"
        claim_key = f"{QUEUE_PREFIX}:claim:{registro_id}"
        now_bogota = datetime.now(ZoneInfo("America/Bogota"))

        if consumir_intento:
            intentos_after = item.intentos + 1
            max_intentos = item.max_intentos or settings.QUEUE_MAX_RETRIES
            es_definitivo = intentos_after >= max_intentos
            espera_minutos = settings.QUEUE_RETRY_INTERVAL_MINUTES * intentos_after
        else:
            intentos_after = item.intentos
            es_definitivo = False
            espera_minutos = settings.QUEUE_RETRY_INTERVAL_MINUTES

        proximo_at = now_bogota + timedelta(minutes=espera_minutos)

        keys = [
            item_key,
            claim_key,
            f"{QUEUE_PREFIX}:status:{SmartStatus.PENDING.value}",
            f"{QUEUE_PREFIX}:status:{SmartStatus.FAILED_FINAL.value}",
            f"{QUEUE_PREFIX}:pending_zset"
        ]
        args = [
            worker_id,
            str(item.version),
            error_msg,
            now_bogota.isoformat(),
            "1" if es_definitivo else "0",
            SmartStatus.FAILED_FINAL.value,
            proximo_at.isoformat(),
            str(proximo_at.timestamp()),
            str(registro_id),
            SmartStatus.PENDING.value,
            "1" if consumir_intento else "0"
        ]

        try:
            raw_res = await self.redis.eval(REGISTRAR_FALLO_LUA_SCRIPT, len(keys), *keys, *args)
            res = json.loads(raw_res)

            if not res.get("success"):
                reason = res.get("reason", "error_desconocido")
                if reason == "not_owner":
                    logger.warning(
                        f"⚠️ [Cola Redis] Registro {registro_id} ya no pertenece al worker {worker_id} "
                        f"al registrar fallo. No se aplica este error sobre el contenido vigente."
                    )
                    return "not_owner"
                if reason == "version_mismatch":
                    logger.warning(
                        f"⚠️ [Cola Redis] Registro {registro_id} fue sobrescrito por un evento más nuevo "
                        f"del mismo caso (versión esperada {item.version}) al registrar fallo. No se "
                        f"aplican intentos/error de un envío anterior sobre el contenido vigente."
                    )
                    return "version_mismatch"
                logger.warning(f"⚠️ [Cola Redis] No se encontró el registro {registro_id} al registrar fallo.")
                return "not_found"

            smart_code = item.smart_code
            if es_definitivo:
                logger.error(f"❌ [Cola Redis] Caso {smart_code} alcanzó el límite máximo de {max_intentos} reintentos.")
                await EmailAlertService.notificar_caso_fallido_definitivo(
                    smart_code=smart_code,
                    total_intentos=intentos_after,
                    ultimo_error=error_msg,
                    correlation_id=item.correlation_id
                )
                if smart_code and item.payload_json:
                    from app.services.idempotency_service import IdempotencyService
                    idempotency_service = IdempotencyService(self.redis)
                    await idempotency_service.liberar_por_fallo_definitivo(smart_code=smart_code, payload_dict=item.payload_json)

            return "failed"

        except Exception as e:
            logger.error(f"Error registrando fallo para registro {registro_id} en Redis: {e}")
            return "not_found"

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

    async def _purgar_estado(self, set_key: str, limite_dt: datetime) -> int:
        """Purga los items de un único set de estado (COMPLETED/FAILED_FINAL) más antiguos que limite_dt."""
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

        if not (a_eliminar or inconsistentes):
            return 0

        async with self._crear_pipeline_compatible() as pipe:
            for item_id in inconsistentes:
                pipe.srem(set_key, item_id)

            for item_id in a_eliminar:
                pipe.delete(f"{QUEUE_PREFIX}:item:{item_id}")
                pipe.srem(set_key, item_id)
                pipe.zrem(f"{QUEUE_PREFIX}:created_zset", item_id)

            await pipe.execute()

        return len(a_eliminar)

    async def purgar_registros_antiguos(
        self,
        dias_retencion: int = settings.QUEUE_RETENTION_DAYS,
        dias_retencion_dlq: int = settings.QUEUE_RETENTION_DAYS_DLQ
    ) -> int:
        if not self.redis:
            return 0

        now_bogota = datetime.now(ZoneInfo("America/Bogota"))
        limite_exitoso = now_bogota - timedelta(days=dias_retencion)
        limite_dlq = now_bogota - timedelta(days=dias_retencion_dlq)

        estados_a_evaluar = [
            (f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", limite_exitoso),
            (f"{QUEUE_PREFIX}:status:{SmartStatus.FAILED_FINAL.value}", limite_dlq)
        ]

        try:
            total_purgados = 0
            for set_key, limite_dt in estados_a_evaluar:
                total_purgados += await self._purgar_estado(set_key, limite_dt)

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

                async with self._crear_pipeline_compatible() as pipe:
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