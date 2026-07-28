# app/services/despacho_queja_orchestrator.py
import logging
from typing import Dict, Any
from pydantic import ValidationError

from app.integrations.sfc_client import SfcClient
from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.services.momento_2_sync import Momento2SincronizacionService
from app.services.momento_3_sync import Momento3SincronizacionService
from app.services.email_service import EmailAlertService
from app.core.exceptions import SfcIntegrationException

logger = logging.getLogger(__name__)


class DespachoQuejaOrquestador:
    """
    Servicio Stateless que actúa como fachada/orquestador único para la transmisión 
    de quejas desde Salesforce hacia la SFC (Momento 2 + Momento 3).
    Incluye lógica de inferencia automática y auto-recuperación (Self-Healing).
    """

    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_client = s3_client
        self.m2_service = Momento2SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
        self.m3_service = Momento3SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)

    async def procesar_despacho_raw_json(self, payload_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Rehidrata un diccionario/JSON desde SQLite al esquema Pydantic 'QuejaUnificadaCrmInput'."""
        try:
            payload = QuejaUnificadaCrmInput.model_validate(payload_dict)
            return await self.procesar_despacho(payload=payload)
        except ValidationError as ve:
            logger.error(f"[Orquestador] Error de validación Pydantic al rehidratar desde la cola SQLite: {ve.json()}")
            raise Exception(f"Estructura inválida en el payload rehidratado de SQLite: {str(ve)}")

    async def procesar_despacho(self, payload: QuejaUnificadaCrmInput) -> Dict[str, Any]:
        smart_code = payload.Smart_Code__c
        status_raw = (payload.Status or "").strip().lower()

        es_cierre = (
            status_raw in ("closed", "cerrado") or 
            payload.ClosedDate is not None or 
            payload.Favorabilidad__c is not None
        )
        es_fraude = (
            payload.tipo_fraude__c is not None or 
            payload.modalidad_fraude__c is not None
        )

        logger.info(
            f"[Orquestador] Procesando solicitud para el caso {smart_code} "
            f"(Es Cierre: {es_cierre}, Es Fraude: {es_fraude}, Status: '{payload.Status}')"
        )

        try:
            # 1. Caso de Alta Nueva Puro (Creación inicial vía Momento 2)
            if not es_cierre and not es_fraude and status_raw in ("new", "nuevo"):
                logger.info(f"[Orquestador] Ejecutando despacho directo de QUEJA NUEVA (M2) para {smart_code}")
                return await self.m2_service.ejecutar_envio_momento_2(payload=payload)

            # 2. Intento inicial de Momento 3 (Trámite, Fraude o Cierre)
            return await self._ejecutar_pasos_momento_3(payload, es_fraude=es_fraude, es_cierre=es_cierre)

        except SfcIntegrationException as exc:
            error_tipo = getattr(exc, "error_type", None)
            is_unmapped = getattr(exc, "is_unmapped", False) or error_tipo == "UNKNOWN_ERROR"
            raw_msg = (getattr(exc, "raw_message", "") or str(exc)).lower()
            
            # 🚨 AUTO-RECUPERACIÓN (SELF-HEALING): 404 Estándar o 404 camuflado en un 400
            es_caso_no_encontrado = (
                exc.status_code == 404 or 
                error_tipo == "NOT_FOUND_ERROR" or 
                "404" in raw_msg or 
                "no encontrado" in raw_msg
            )

            if es_caso_no_encontrado:
                logger.warning(
                    f"⚠️ [Orquestador] La SFC indica que la queja {smart_code} NO existe en su BD. "
                    f"Iniciando secuencia de auto-recuperación (Momento 2 -> Momento 3)..."
                )

                # Paso A: Crear la queja base en la SFC vía Momento 2
                logger.info(f"[Auto-Recuperación 1/2] Radicando queja base vía Momento 2 para {smart_code}...")
                res_m2 = await self.m2_service.ejecutar_envio_momento_2(payload=payload)

                if res_m2.get("status") != "success":
                    return res_m2

                # Paso B: Aplicar la actualización de Momento 3
                logger.info(f"[Auto-Recuperación 2/2] Re-ejecutando pipeline de Momento 3 para {smart_code}...")
                return await self._ejecutar_pasos_momento_3(payload, es_fraude=es_fraude, es_cierre=es_cierre)
            
            # ⚠️ EVALUACIÓN DE ERROR NO MAPEADO (NOTIFICAR A DEV)
            if is_unmapped:
                logger.warning(f"⚠️ [Orquestador] Se detectó un error no mapeado en SFC para el caso {smart_code}.")
                await EmailAlertService.notificar_error_no_mapeado(
                    status_code=getattr(exc, "status_code", 500),
                    raw_message=str(exc),
                    sfc_field=getattr(exc, "sfc_field", None),
                    smart_code=smart_code
                )

            raise

    async def _ejecutar_pasos_momento_3(
        self, 
        payload: QuejaUnificadaCrmInput, 
        es_fraude: bool, 
        es_cierre: bool
    ) -> Dict[str, Any]:
        resultado = {}

        if es_fraude:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo gestión de FRAUDE para {payload.Smart_Code__c}...")
            resultado = await self.m3_service.ejecutar_gestion_fraude(payload=payload)

        if es_cierre:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo CIERRE DEFINITIVO para {payload.Smart_Code__c}...")
            resultado = await self.m3_service.ejecutar_cierre_definitivo(payload=payload)

        if not es_fraude and not es_cierre:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo ACTUALIZACIÓN DE TRÁMITE para {payload.Smart_Code__c}...")
            resultado = await self.m3_service.ejecutar_actualizacion_tramite(payload=payload)

        return resultado