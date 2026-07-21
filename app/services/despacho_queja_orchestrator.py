import logging
from typing import Dict, Any
from pydantic import ValidationError

from app.integrations.sfc_client import SfcClient
from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.services.momento_2_sync import Momento2SincronizacionService
from app.services.momento_3_sync import Momento3SincronizacionService
from app.core.exceptions import SfcIntegrationException

logger = logging.getLogger(__name__)

class DespachoQuejaOrquestador:
    """
    Servicio Stateless que actúa como fachada/orquestador único para la transmisión 
    de quejas desde Salesforce hacia la SFC (Momento 2 + Momento 3).
    """

    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_client = s3_client
        self.m2_service = Momento2SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
        self.m3_service = Momento3SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)

    async def procesar_despacho_raw_json(self, payload_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Método consumido principalmente por el Scheduler de reintentos.
        Rehidrata un diccionario/JSON desde SQLite al esquema Pydantic 'QuejaUnificadaCrmInput'
        y ejecuta el flujo de despacho correspondiente.
        """
        try:
            payload = QuejaUnificadaCrmInput.model_validate(payload_dict)
            return await self.procesar_despacho(payload=payload)
        except ValidationError as ve:
            logger.error(f"[Orquestador] Error de validación Pydantic al rehidratar desde la cola SQLite: {ve.json()}")
            raise Exception(f"Estructura inválida en el payload rehidratado de SQLite: {str(ve)}")

    async def procesar_despacho(self, payload: QuejaUnificadaCrmInput) -> Dict[str, Any]:
        """
        Determina dinámicamente si el despacho corresponde a un alta nueva (M2), 
        una actualización de trámite (M3), reporte de fraude (M3) o cierre definitivo (M3).
        """
        smart_code = payload.Smart_Code__c
        logger.info(f"[Orquestador] Procesando solicitud unificada para el caso: {smart_code}")

        # 🧭 DETERMINACIÓN DE EVENTO
        # 1. Si se solicita explícitamente o si es Cierre Definitivo
        if payload.tipo_operacion == "CIERRE" or payload.Status == "Closed" or payload.ClosedDate is not None:
            logger.info(f"[Orquestador] Detectada operación de CIERRE DEFINITIVO (M3) para {smart_code}")
            return await self.m3_service.ejecutar_cierre_definitivo(payload=payload)

        # 2. Si se reporta o actualiza Investigación de Fraude
        if payload.tipo_operacion == "FRAUDE" or payload.tipo_fraude__c is not None or payload.modalidad_fraude__c is not None:
            logger.info(f"[Orquestador] Detectada operación de INVESTIGACIÓN DE FRAUDE (M3) para {smart_code}")
            return await self.m3_service.ejecutar_gestion_fraude(payload=payload)

        # 3. Si se solicita explícitamente una actualización de trámite
        if payload.tipo_operacion == "TRAMITE":
            logger.info(f"[Orquestador] Detectada actualización explícita de TRÁMITE (M3) para {smart_code}")
            return await self.m3_service.ejecutar_actualizacion_tramite(payload=payload)

        # 4. Si el estado es "New" / Alta Inicial -> Momento 2 (Creación)
        if payload.tipo_operacion == "NUEVA" or payload.Status == "New":
            logger.info(f"[Orquestador] Detectado despacho de QUEJA NUEVA (M2) para {smart_code}")
            return await self.m2_service.ejecutar_envio_momento_2(payload=payload)

        # 5. Fallback por defecto: Trámite/Actualización M3
        logger.info(f"[Orquestador] Inferencia por defecto a ACTUALIZACIÓN DE TRÁMITE (M3) para {smart_code}")
        return await self.m3_service.ejecutar_actualizacion_tramite(payload=payload)