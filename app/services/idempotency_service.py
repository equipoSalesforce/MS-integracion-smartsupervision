# app/services/idempotency_service.py
import asyncio
import json
import hashlib
import logging
from datetime import datetime
from typing import Tuple, Optional, Dict, Any, Set
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.constants import SmartStatus
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)

IDEMPOTENCY_PREFIX = "{sfc:idempotency}"

# 🟢 FIX P1-13: el candado PROCESSING se creaba con un TTL fijo de 3 minutos y sin
# renovación. Un despacho legítimo que tardara más que eso (SFC lenta, reintentos
# internos del cliente HTTP) dejaba expirar el candado a mitad de vuelo: un reintento
# del mismo payload durante esa ventana ya no vería "processing" y dispararía un
# segundo envío concurrente a la SFC. Se extiende el TTL sólo si el registro sigue
# siendo PROCESSING y corresponde a ESTE payload_hash, para no revivir un candado
# ajeno que ya fue liberado/reemplazado.
EXTEND_PROCESSING_LUA_SCRIPT = """
local raw = redis.call("get", KEYS[1])
if not raw then
    return 0
end
local ok, record = pcall(cjson.decode, raw)
if not ok or type(record) ~= "table" then
    return 0
end
if record["status"] == "PROCESSING" and record["payload_hash"] == ARGV[1] then
    return redis.call("pexpire", KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


class IdempotencyProcessingHeartbeat:
    """
    Context manager que mantiene vivo el candado PROCESSING de idempotencia mientras
    dura el trabajo real (llamada a la SFC), renovando su TTL periódicamente en vez de
    depender de un TTL fijo que puede expirar antes de que termine el despacho.
    """

    def __init__(
        self,
        redis_client,
        key: str,
        payload_hash: str,
        lease_ms: int = 180000,
        intervalo_segundos: int = 45
    ):
        self.redis = redis_client
        self.key = key
        self.payload_hash = payload_hash
        self.lease_ms = lease_ms
        self.intervalo_segundos = intervalo_segundos
        self._task: Optional[asyncio.Task] = None

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self.intervalo_segundos)
            try:
                await self.redis.eval(
                    EXTEND_PROCESSING_LUA_SCRIPT,
                    1,
                    self.key,
                    self.payload_hash,
                    str(self.lease_ms)
                )
            except Exception as e:
                logger.warning(f"⚠️ [Idempotency Heartbeat] No se pudo renovar el candado '{self.key}': {e}")

    async def __aenter__(self):
        if self.redis:
            self._task = asyncio.create_task(self._heartbeat())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class IdempotencyService:
    """
    Servicio de Almacén de Idempotencia (Idempotency Store) independiente de la Cola de Reintentos.
    Garantiza que peticiones repetidas con el MISMO payload en el 'Camino Exitoso' (Happy Path),
    en proceso o encoladas retornen la respuesta correspondiente sin volver a procesar/llamar a la SFC.
    
    🛡️ POLÍTICA FAIL-CLOSED: Si Redis no está disponible, bloquea operaciones mutativas para 
    prevenir transmisiones duplicadas hacia la SFC.
    """

    # 🟢 FIX P0-11: campos que el esquema Pydantic auto-rellena con la fecha/hora ACTUAL
    # cuando el cliente los omite (ver `auto_completar_y_validar_fecha_creacion` para
    # CreatedDate, mode="before"; y el auto-relleno de ClosedDate en
    # `validar_reglas_segun_datos_presentes`). Si participaran en el hash, dos envíos
    # IDÉNTICOS del mismo request (mismo campo omitido) generarían hashes distintos según
    # el instante exacto de procesamiento de cada uno, rompiendo la deduplicación de
    # idempotencia justo para el caso que más importa: un reintento genuino del mismo
    # request. Se excluyen del hash para todos los llamadores por igual.
    CAMPOS_EXCLUIDOS_DEL_HASH = {"CreatedDate", "ClosedDate"}

    def __init__(self, redis_client=None, ttl_days: int = 30):
        self.redis = redis_client
        self.ttl_seconds = ttl_days * 86400

    @staticmethod
    def compute_payload_hash(payload_dict: dict) -> str:
        """
        Genera un hash SHA-256 determinista de un payload CANÓNICO: ordena las llaves y
        excluye los campos con auto-relleno no determinista (🟢 FIX P0-11).
        """
        canonico = {
            k: v for k, v in payload_dict.items()
            if k not in IdempotencyService.CAMPOS_EXCLUIDOS_DEL_HASH
        }
        serialized = json.dumps(canonico, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _get_idempotency_key(self, smart_code: str, operation: str, payload_hash: str) -> str:
        return f"{IDEMPOTENCY_PREFIX}:{smart_code}:{operation}:{payload_hash}"

    @staticmethod
    def infer_operation_type(payload_dict: dict) -> str:
        """Infiere la fase/operación normativa basada en la estructura del DTO."""
        status_clean = (payload_dict.get("Status") or "").strip().lower()
        
        es_cierre = (
            status_clean in ("closed", "cerrado") or 
            payload_dict.get("ClosedDate") is not None or 
            payload_dict.get("Favorabilidad__c") is not None
        )
        es_fraude = (
            payload_dict.get("tipo_fraude__c") is not None or 
            payload_dict.get("modalidad_fraude__c") is not None
        )
        tiene_campos_m3 = any([
            payload_dict.get("sc_genero__c") is not None,
            payload_dict.get("sc_LGBTIQ__c") is not None,
            payload_dict.get("sc_Condicion_especial__c") is not None,
            payload_dict.get("producto_digital__c") is not None,
            payload_dict.get("admision_col__c") not in (None, "No Aplica")
        ])
        
        es_m2_puro = (status_clean in ("new", "nuevo")) and not tiene_campos_m3 and not es_fraude and not es_cierre

        if es_m2_puro:
            return "M2_CREATION"
        elif es_cierre and es_fraude:
            return "M3_FRAUD_AND_CLOSE"
        elif es_fraude:
            return "M3_FRAUD"
        elif es_cierre:
            return "M3_CLOSE"
        else:
            return "M3_UPDATE"

    def mantener_processing_vivo(self, smart_code: str, payload_dict: dict) -> "IdempotencyProcessingHeartbeat":
        """
        🟢 FIX P1-13: construye el context manager que renueva el candado PROCESSING
        mientras dura el despacho real. Usar así:
            async with idempotency_service.mantener_processing_vivo(smart_code, raw_payload):
                ... llamada real a la SFC ...
        """
        operation = self.infer_operation_type(payload_dict)
        payload_hash = self.compute_payload_hash(payload_dict)
        key = self._get_idempotency_key(smart_code, operation, payload_hash)
        return IdempotencyProcessingHeartbeat(redis_client=self.redis, key=key, payload_hash=payload_hash)

    async def verificar_o_iniciar_operacion(
        self,
        smart_code: str,
        payload_dict: dict,
        source: str = "CRM_SALESFORCE",
        fail_closed: bool = True
    ) -> Tuple[bool, Optional[dict]]:
        operation = self.infer_operation_type(payload_dict)
        payload_hash = self.compute_payload_hash(payload_dict)
        
        # 🚨 FAIL-CLOSED 1: Cliente de Redis en None / No Inicializado
        if not self.redis:
            if fail_closed:
                logger.critical(
                    f"🚨 [Fail-Closed] Redis no disponible al verificar idempotencia para {smart_code}. "
                    f"Bloqueando transmisión mutativa a la SFC."
                )
                await EmailAlertService.notificar_falla_infraestructura(
                    smart_code=smart_code,
                    error_msg="Fail-Closed: Servidor de Idempotencia (Redis) no disponible/incalcanzable."
                )
                return True, {
                    "status": "redis_unavailable",
                    "status_code": 503,
                    "is_idempotent_hit": False,
                    "smart_code": smart_code,
                    "operation": operation,
                    "error_type": "IDEMPOTENCY_STORE_UNAVAILABLE",
                    "message": "El servicio de validación de idempotencia no está disponible. La operación fue bloqueada (Fail-Closed) para prevenir duplicaciones en la SFC."
                }
            return False, None

        key = self._get_idempotency_key(smart_code, operation, payload_hash)
        now_iso = datetime.now(ZoneInfo("America/Bogota")).isoformat()

        try:
            raw_record = await self.redis.get(key)

            if raw_record and isinstance(raw_record, (str, bytes)):
                record = json.loads(raw_record)
                op_status = record.get("status")
                rec_hash = record.get("payload_hash")

                if rec_hash == payload_hash:
                    if op_status == "COMPLETED":
                        logger.info(
                            f"🎯 [Idempotency Store] Hit exitoso previo para {smart_code} "
                            f"({operation} | Hash: {payload_hash[:8]}). Retornando respuesta almacenada."
                        )
                        return True, {
                            "status": "success",
                            "is_idempotent_hit": True,
                            "smart_code": smart_code,
                            "operation": operation,
                            "payload_hash": payload_hash,
                            "message": "Operación ya procesada exitosamente con anterioridad para este mismo payload.",
                            "sfc_response": record.get("sfc_response")
                        }

                    if op_status == "PROCESSING":
                        logger.warning(
                            f"⏳ [Idempotency Store] La operación '{operation}' para {smart_code} "
                            f"(Hash: {payload_hash[:8]}) ya está en proceso."
                        )
                        return True, {
                            "status": "processing",
                            "is_idempotent_hit": True,
                            "smart_code": smart_code,
                            "operation": operation,
                            "payload_hash": payload_hash,
                            "message": f"La operación '{operation}' para el caso {smart_code} ya está siendo procesada."
                        }

                    if op_status == "QUEUED":
                        # 🟢 FIX P0-12: antes de reportar "ya encolado", verificar que el item
                        # de cola referenciado siga existiendo, pendiente, y con ESTE mismo
                        # payload. Si otro evento del mismo smart_code lo sobrescribió (ver
                        # ENQUEUE_LUA_SCRIPT), este registro quedó huérfano: liberarlo y dejar
                        # que la operación se procese como nueva, en vez de mentir que sigue
                        # encolada cuando en realidad nunca se procesará en esta forma.
                        sigue_vigente = await self._item_de_cola_sigue_vigente(
                            record.get("queue_item_id"), payload_hash
                        )

                        if sigue_vigente:
                            logger.info(
                                f"📦 [Idempotency Store] La operación '{operation}' para {smart_code} "
                                f"(Hash: {payload_hash[:8]}) ya está encolada."
                            )
                            return True, {
                                "status": "already_queued",
                                "is_idempotent_hit": True,
                                "smart_code": smart_code,
                                "operation": operation,
                                "payload_hash": payload_hash,
                                "message": f"El caso {smart_code} ya se encuentra encolado en la cola de contingencia."
                            }

                        logger.warning(
                            f"⚠️ [Idempotency Store] Registro QUEUED huérfano para {smart_code} "
                            f"({operation} | Hash: {payload_hash[:8]}): el item de cola "
                            f"{record.get('queue_item_id')} ya no contiene este payload. Liberando "
                            f"y procesando como una operación nueva."
                        )
                        try:
                            await self.redis.delete(key)
                        except Exception as del_err:
                            logger.warning(f"No se pudo liberar el registro QUEUED huérfano para {smart_code}: {del_err}")
                        # No retorna: cae hacia abajo para iniciar como PROCESSING nuevo.

            initial_record = {
                "source": source,
                "smart_code": smart_code,
                "operation": operation,
                "payload_hash": payload_hash,
                "status": "PROCESSING",
                "created_at": now_iso,
                "completed_at": None,
                "sfc_response": None
            }

            set_success = await self.redis.set(
                key, 
                json.dumps(initial_record, ensure_ascii=False), 
                px=180000, 
                nx=True
            )

            if not set_success:
                check_record = await self.redis.get(key)
                if check_record:
                    rec_json = json.loads(check_record)
                    if rec_json.get("status") == "COMPLETED" and rec_json.get("payload_hash") == payload_hash:
                        return True, {
                            "status": "success",
                            "is_idempotent_hit": True,
                            "smart_code": smart_code,
                            "operation": operation,
                            "payload_hash": payload_hash,
                            "sfc_response": rec_json.get("sfc_response")
                        }

                return True, {
                    "status": "processing",
                    "is_idempotent_hit": True,
                    "smart_code": smart_code,
                    "operation": operation,
                    "payload_hash": payload_hash,
                    "message": f"La operación '{operation}' para el caso {smart_code} ya está siendo procesada."
                }

            return False, None

        # 🚨 FAIL-CLOSED 2: Excepción de Socket / Timeout / Caída de Redis durante la ejecución
        except Exception as e:
            logger.critical(f"🚨 [Fail-Closed] Excepción al consultar Idempotency Store en Redis para {smart_code}: {e}")
            if fail_closed:
                await EmailAlertService.notificar_falla_infraestructura(
                    smart_code=smart_code,
                    error_msg=f"Fail-Closed: Error consultando almacén de idempotencia (Redis): {str(e)}"
                )
                return True, {
                    "status": "redis_unavailable",
                    "status_code": 503,
                    "is_idempotent_hit": False,
                    "smart_code": smart_code,
                    "operation": operation,
                    "error_type": "IDEMPOTENCY_STORE_UNAVAILABLE",
                    "message": "Error al consultar el almacén de idempotencia. Operación bloqueada (Fail-Closed) para prevenir duplicaciones en la SFC."
                }
            return False, None

    async def registrar_exito(
        self,
        smart_code: str,
        payload_dict: dict,
        sfc_response: dict,
        max_intentos_persistencia: int = 3
    ):
        """
        🟢 FIX P0-06: registrar COMPLETED aquí es lo que evita que una petición fresca
        duplicada (misma operación/payload) vuelva a disparar la SFC por la vía síncrona.
        Ya no se traga la excepción de Redis: reintenta con backoff corto y, si sigue
        fallando, propaga para que el caller lo trate como falla crítica de infraestructura.
        """
        if not self.redis:
            raise RuntimeError("Cliente de Redis no disponible al intentar registrar éxito de idempotencia.")

        operation = self.infer_operation_type(payload_dict)
        payload_hash = self.compute_payload_hash(payload_dict)
        key = self._get_idempotency_key(smart_code, operation, payload_hash)
        now_iso = datetime.now(ZoneInfo("America/Bogota")).isoformat()

        record = {
            "source": "CRM_SALESFORCE",
            "smart_code": smart_code,
            "operation": operation,
            "payload_hash": payload_hash,
            "status": "COMPLETED",
            "created_at": now_iso,
            "completed_at": now_iso,
            "sfc_response": sfc_response
        }

        ultimo_error: Optional[Exception] = None
        for intento in range(1, max_intentos_persistencia + 1):
            try:
                await self.redis.set(key, json.dumps(record, ensure_ascii=False), px=self.ttl_seconds * 1000)
                logger.info(
                    f"✅ [Idempotency Store] Operación '{operation}' para {smart_code} "
                    f"(Hash: {payload_hash[:8]}) registrada como COMPLETED."
                )
                return
            except Exception as e:
                ultimo_error = e
                logger.warning(
                    f"⚠️ [Idempotency Store] Intento {intento}/{max_intentos_persistencia} fallido "
                    f"registrando éxito para {smart_code}: {e}"
                )
                if intento < max_intentos_persistencia:
                    await asyncio.sleep(0.5 * intento)

        raise RuntimeError(
            f"No fue posible registrar éxito de idempotencia para {smart_code} tras "
            f"{max_intentos_persistencia} intentos. Último error: {ultimo_error}"
        )

    async def registrar_encolado(
        self,
        smart_code: str,
        payload_dict: dict,
        error_msg: str,
        registro_id: Optional[int] = None,
        max_intentos_persistencia: int = 3
    ):
        """
        🟢 FIX P0-12: se guarda `queue_item_id` (el id del item de cola real) junto con el
        estado QUEUED, para poder verificar más adelante si ese item sigue conteniendo
        este mismo payload — o si fue sobrescrito por un evento más nuevo del mismo
        smart_code y este registro de idempotencia quedó huérfano.

        🟢 FIX P0-03 (auditoría adversarial v9): antes, si el SET a Redis fallaba, la
        excepción se registraba sólo en log y el método retornaba normalmente — el
        endpoint (routes_quejas.py) devolvía 202 "encolado" como si la barrera de
        idempotencia QUEUED estuviera activa, cuando en realidad nunca se persistió.
        Un retry del mismo request durante esa ventana no tenía protección y podía
        competir con el item ya encolado. Ahora reintenta con backoff corto (mismo
        patrón que registrar_exito) y, si sigue fallando, propaga: el caller ya
        envuelve esta llamada en el mismo try/except que usa para el fallo doble
        SFC+Redis de encolar_despacho, así que la excepción hace que el endpoint
        responda 503 (fallo real) en vez de un 202 falsamente protegido.
        """
        if not self.redis:
            raise RuntimeError("Cliente de Redis no disponible al registrar estado QUEUED en Idempotency Store.")

        operation = self.infer_operation_type(payload_dict)
        payload_hash = self.compute_payload_hash(payload_dict)
        key = self._get_idempotency_key(smart_code, operation, payload_hash)
        now_iso = datetime.now(ZoneInfo("America/Bogota")).isoformat()

        record = {
            "source": "CRM_SALESFORCE",
            "smart_code": smart_code,
            "operation": operation,
            "payload_hash": payload_hash,
            "status": "QUEUED",
            "created_at": now_iso,
            "completed_at": None,
            "sfc_response": None,
            "error_msg": error_msg,
            "queue_item_id": registro_id
        }

        ultimo_error: Optional[Exception] = None
        for intento in range(1, max_intentos_persistencia + 1):
            try:
                await self.redis.set(key, json.dumps(record, ensure_ascii=False), px=self.ttl_seconds * 1000)
                return
            except Exception as e:
                ultimo_error = e
                logger.warning(
                    f"⚠️ [Idempotency Store] Intento {intento}/{max_intentos_persistencia} fallido registrando "
                    f"estado QUEUED para {smart_code}: {e}"
                )
                if intento < max_intentos_persistencia:
                    await asyncio.sleep(0.5 * intento)

        raise RuntimeError(
            f"No fue posible registrar estado QUEUED en Idempotency Store para {smart_code} tras "
            f"{max_intentos_persistencia} intentos. Último error: {ultimo_error}"
        )

    async def _item_de_cola_sigue_vigente(self, queue_item_id: Optional[int], payload_hash: str) -> bool:
        """
        🟢 FIX P0-12: verifica que el item de cola referenciado por un registro QUEUED
        todavía exista, siga pendiente, y su payload actual corresponda al MISMO hash —
        es decir, que no haya sido sobrescrito por un evento más nuevo del mismo
        smart_code (ver ENQUEUE_LUA_SCRIPT en queue_service.py, que reutiliza el mismo
        item_id al colisionar). No se importa QueueService/QUEUE_PREFIX a nivel de módulo
        para evitar el ciclo de imports ya existente entre este servicio y queue_service.
        """
        if not queue_item_id or not self.redis:
            # Registros QUEUED previos a este fix no tienen queue_item_id almacenado;
            # se asume vigente para no romper items ya en vuelo al desplegar el cambio.
            return True
        try:
            raw_item = await self.redis.get(f"{{sfc:queue}}:item:{queue_item_id}")
            if not raw_item:
                return False
            data = json.loads(raw_item)
            if data.get("estado") != SmartStatus.PENDING.value:
                return False
            item_hash = self.compute_payload_hash(data.get("payload_json") or {})
            return item_hash == payload_hash
        except Exception as e:
            logger.warning(f"No se pudo verificar vigencia del item de cola {queue_item_id}: {e}")
            return True  # Fail-open: ante la duda, no se altera el comportamiento previo.

    async def liberar_por_fallo_definitivo(self, smart_code: str, payload_dict: dict):
        if not self.redis:
            return

        operation = self.infer_operation_type(payload_dict)
        payload_hash = self.compute_payload_hash(payload_dict)
        key = self._get_idempotency_key(smart_code, operation, payload_hash)

        try:
            await self.redis.delete(key)
            logger.info(
                f"🧹 [Idempotency Store] Registro de idempotencia '{operation}' "
                f"(Hash: {payload_hash[:8]}) liberado para {smart_code} tras falla definitiva (DLQ)."
            )
        except Exception as e:
            logger.error(f"Error liberando registro de idempotencia por fallo definitivo para {smart_code}: {e}")

    async def liberar_operacion_por_error(self, smart_code: str, payload_dict: dict):
        if not self.redis:
            return

        operation = self.infer_operation_type(payload_dict)
        payload_hash = self.compute_payload_hash(payload_dict)
        key = self._get_idempotency_key(smart_code, operation, payload_hash)

        try:
            await self.redis.delete(key)
            logger.info(
                f"🧹 [Idempotency Store] Candado '{operation}' (Hash: {payload_hash[:8]}) "
                f"liberado para {smart_code} debido a error de validación o excepción."
            )
        except Exception as e:
            logger.error(f"Error liberando candado de idempotencia por error para {smart_code}: {e}")

    # ==========================================================================
    # 🟢 FIX P0-10: CHECKPOINT DURABLE POR ARCHIVO/PASO
    # ==========================================================================
    # Complementa la idempotencia a nivel de request completo: registra qué archivos
    # individuales ya fueron confirmados como transmitidos a la SFC para un caso, para
    # que un reintento del LOTE completo (tras un fallo parcial) no vuelva a reenviar
    # los que ya tuvieron éxito. Antes de esto, la única protección era la detección de
    # duplicados del propio lado de la SFC (best-effort, por coincidencia de texto).

    def _get_checkpoint_key(self, sfc_codigo_queja: str) -> str:
        return f"{IDEMPOTENCY_PREFIX}:file_checkpoint:{sfc_codigo_queja}"

    async def obtener_archivos_completados(self, sfc_codigo_queja: str) -> Set[str]:
        """Devuelve el conjunto de identificadores de archivo (s3_key) ya confirmados
        como transmitidos exitosamente a la SFC para este caso."""
        if not self.redis:
            return set()
        key = self._get_checkpoint_key(sfc_codigo_queja)
        try:
            raw_keys = await self.redis.hkeys(key)
            return {k if isinstance(k, str) else k.decode("utf-8") for k in raw_keys}
        except Exception as e:
            # Fail-open deliberado: en el peor caso se reintenta un archivo que ya había
            # tenido éxito (el comportamiento previo a este fix), no se pierde ni duplica
            # nada nuevo — la protección de fondo sigue siendo la deduplicación de la SFC.
            logger.error(f"Error consultando checkpoint de archivos para {sfc_codigo_queja}: {e}")
            return set()

    async def marcar_archivo_completado(
        self, sfc_codigo_queja: str, identificador_archivo: str, metadata: Optional[dict] = None
    ):
        """Registra que un archivo específico ya fue transmitido exitosamente a la SFC.
        Se llama INMEDIATAMENTE tras el éxito de CADA archivo (no al final del lote),
        para no perder el progreso ya confirmado si otro archivo del mismo lote falla
        después."""
        if not self.redis:
            return
        key = self._get_checkpoint_key(sfc_codigo_queja)
        try:
            valor = json.dumps(metadata or {"completed_at": datetime.now(ZoneInfo("America/Bogota")).isoformat()}, ensure_ascii=False)
            await self.redis.hset(key, identificador_archivo, valor)
            await self.redis.expire(key, self.ttl_seconds)
        except Exception as e:
            logger.error(
                f"⚠️ [Idempotency Store] No se pudo persistir el checkpoint del archivo "
                f"'{identificador_archivo}' para {sfc_codigo_queja}: {e}. "
                f"El archivo SÍ fue transmitido a la SFC; un reintento podría reenviarlo "
                f"y depender de la deduplicación del lado de la SFC."
            )

    async def limpiar_checkpoint_archivos(self, sfc_codigo_queja: str):
        """Libera el checkpoint una vez que el caso completó su ciclo (éxito definitivo o
        fallo definitivo), para no acumular claves indefinidamente."""
        if not self.redis:
            return
        key = self._get_checkpoint_key(sfc_codigo_queja)
        try:
            await self.redis.delete(key)
        except Exception as e:
            logger.error(f"Error liberando checkpoint de archivos para {sfc_codigo_queja}: {e}")