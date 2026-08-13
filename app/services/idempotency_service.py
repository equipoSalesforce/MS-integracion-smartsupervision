# app/services/idempotency_service.py
import json
import hashlib
import logging
from datetime import datetime
from typing import Tuple, Optional, Dict, Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)

IDEMPOTENCY_PREFIX = "{sfc:idempotency}"


class IdempotencyService:
    """
    Servicio de Almacén de Idempotencia (Idempotency Store) independiente de la Cola de Reintentos.
    Garantiza que peticiones repetidas con el MISMO payload en el 'Camino Exitoso' (Happy Path),
    en proceso o encoladas retornen la respuesta correspondiente sin volver a procesar/llamar a la SFC.
    
    🛡️ POLÍTICA FAIL-CLOSED: Si Redis no está disponible, bloquea operaciones mutativas para 
    prevenir transmisiones duplicadas hacia la SFC.
    """

    def __init__(self, redis_client=None, ttl_days: int = 30):
        self.redis = redis_client
        self.ttl_seconds = ttl_days * 86400

    @staticmethod
    def compute_payload_hash(payload_dict: dict) -> str:
        """Genera un hash SHA-256 determinista del payload ordenando sus llaves."""
        serialized = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)
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

    async def registrar_exito(self, smart_code: str, payload_dict: dict, sfc_response: dict):
        if not self.redis:
            return

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

        try:
            await self.redis.set(key, json.dumps(record, ensure_ascii=False), px=self.ttl_seconds * 1000)
            logger.info(
                f"✅ [Idempotency Store] Operación '{operation}' para {smart_code} "
                f"(Hash: {payload_hash[:8]}) registrada como COMPLETED."
            )
        except Exception as e:
            logger.error(f"Error registrando éxito en Idempotency Store para {smart_code}: {e}")

    async def registrar_encolado(self, smart_code: str, payload_dict: dict, error_msg: str):
        if not self.redis:
            return

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
            "error_msg": error_msg
        }

        try:
            await self.redis.set(key, json.dumps(record, ensure_ascii=False), px=self.ttl_seconds * 1000)
        except Exception as e:
            logger.error(f"Error registrando estado QUEUED en Idempotency Store para {smart_code}: {e}")

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