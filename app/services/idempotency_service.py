# app/services/idempotency_service.py
import json
import hashlib
import logging
from datetime import datetime
from typing import Tuple, Optional, Dict, Any
from zoneinfo import ZoneInfo

from app.core.config import settings

logger = logging.getLogger(__name__)


class IdempotencyService:
    """
    Servicio de Almacén de Idempotencia (Idempotency Store) independiente de la Cola de Reintentos.
    Garantiza que peticiones repetidas en el 'Camino Exitoso' (Happy Path) o en proceso
    retornen la respuesta exacta almacenada sin volver a llamar a la SFC.
    """

    def __init__(self, redis_client=None, ttl_days: int = 30):
        self.redis = redis_client
        self.ttl_seconds = ttl_days * 86400

    @staticmethod
    def compute_payload_hash(payload_dict: dict) -> str:
        """Genera un hash SHA-256 determinista del payload ordenando sus llaves."""
        serialized = json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

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
        source: str = "CRM_SALESFORCE"
    ) -> Tuple[bool, Optional[dict]]:
        """
        Consulta si la operación ya fue completada exitosamente previamente, está en proceso o encolada.
        Retorna: (es_idempotente_hit: bool, respuesta_o_mensaje: Optional[dict])
        """
        if not self.redis:
            return False, None

        operation = self.infer_operation_type(payload_dict)
        payload_hash = self.compute_payload_hash(payload_dict)
        key = f"sfc:idempotency:{smart_code}:{operation}"
        now_iso = datetime.now(ZoneInfo("America/Bogota")).isoformat()

        try:
            raw_record = await self.redis.get(key)

            if raw_record and isinstance(raw_record, (str, bytes)):
                record = json.loads(raw_record)
                op_status = record.get("status")

                # 🎯 1. HIT EXITOSO PREVIO (Happy Path duplicado) -> Retornar respuesta original
                if op_status == "COMPLETED":
                    logger.info(f"🎯 [Idempotency Store] Hit exitoso previo para {smart_code} ({operation}). Retornando respuesta almacenada.")
                    return True, {
                        "status": "success",
                        "is_idempotent_hit": True,
                        "smart_code": smart_code,
                        "operation": operation,
                        "message": "Operación ya procesada exitosamente con anterioridad.",
                        "sfc_response": record.get("sfc_response")
                    }

                # 🎯 2. PETICIÓN PARALELA EN CURSO
                if op_status == "PROCESSING":
                    logger.warning(f"⏳ [Idempotency Store] La operación '{operation}' para {smart_code} está en proceso.")
                    return True, {
                        "status": "processing",
                        "is_idempotent_hit": True,
                        "smart_code": smart_code,
                        "message": f"La operación '{operation}' para el caso {smart_code} ya está siendo procesada."
                    }

                # 🎯 3. CASO YA ENCOLADO EN REDIS QUEUE
                if op_status == "QUEUED":
                    logger.info(f"📦 [Idempotency Store] La operación '{operation}' para {smart_code} ya está encolada.")
                    return True, {
                        "status": "already_queued",
                        "is_idempotent_hit": True,
                        "smart_code": smart_code,
                        "message": f"El caso {smart_code} ya se encuentra encolado en la cola de contingencia."
                    }

            # 🎯 CASO NUEVO: Registrar estado "PROCESSING" con bloqueo atómico de 60 segundos
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

            await self.redis.set(key, json.dumps(initial_record, ensure_ascii=False), px=60000)
            return False, None

        except Exception as e:
            logger.error(f"Error consultando Idempotency Store en Redis: {e}")
            return False, None

    async def registrar_exito(self, smart_code: str, payload_dict: dict, sfc_response: dict):
        """Registra la operación como COMPLETED con su respuesta oficial y TTL de 30 días."""
        if not self.redis:
            return

        operation = self.infer_operation_type(payload_dict)
        key = f"sfc:idempotency:{smart_code}:{operation}"
        now_iso = datetime.now(ZoneInfo("America/Bogota")).isoformat()

        record = {
            "source": "CRM_SALESFORCE",
            "smart_code": smart_code,
            "operation": operation,
            "payload_hash": self.compute_payload_hash(payload_dict),
            "status": "COMPLETED",
            "created_at": now_iso,
            "completed_at": now_iso,
            "sfc_response": sfc_response
        }

        try:
            await self.redis.set(key, json.dumps(record, ensure_ascii=False), px=self.ttl_seconds * 1000)
            logger.info(f"✅ [Idempotency Store] Operación '{operation}' para {smart_code} registrada como COMPLETED.")
        except Exception as e:
            logger.error(f"Error registrando éxito en Idempotency Store para {smart_code}: {e}")

    async def registrar_encolado(self, smart_code: str, payload_dict: dict, error_msg: str):
        """Registra la operación como QUEUED cuando la SFC no está disponible."""
        if not self.redis:
            return

        operation = self.infer_operation_type(payload_dict)
        key = f"sfc:idempotency:{smart_code}:{operation}"
        now_iso = datetime.now(ZoneInfo("America/Bogota")).isoformat()

        record = {
            "source": "CRM_SALESFORCE",
            "smart_code": smart_code,
            "operation": operation,
            "payload_hash": self.compute_payload_hash(payload_dict),
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