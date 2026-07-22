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
    Incluye lógica de inferencia automática y auto-recuperación (Self-Healing).
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
        Infiere dinámicamente el flujo adecuado (M2 o M3) sin depender de 'tipo_operacion'.
        Si la SFC responde que la queja no existe en su base de datos (HTTP 404 / NOT_FOUND_ERROR),
        activa el pipeline de auto-recuperación: la crea vía Momento 2 y luego reintenta el Momento 3.
        """
        smart_code = payload.Smart_Code__c
        
        # 🧭 INFERENCIA BASADA EXCLUSIVAMENTE EN PRESENCIA DE DATOS
        es_cierre = (
            payload.Status in ("Closed", "Cerrado") or 
            payload.ClosedDate is not None or 
            payload.Favorabilidad__c is not None
        )
        es_fraude = (
            payload.tipo_fraude__c is not None or 
            payload.modalidad_fraude__c is not None
        )

        logger.info(
            f"[Orquestador] Procesando solicitud para el caso {smart_code} "
            f"(Es Cierre: {es_cierre}, Es Fraude: {es_fraude})"
        )

        # 1. Caso de Alta Nueva Puro (Creación inicial vía Momento 2)
        if not es_cierre and not es_fraude and payload.Status in ("New", "Nuevo"):
            logger.info(f"[Orquestador] Ejecutando despacho directo de QUEJA NUEVA (M2) para {smart_code}")
            return await self.m2_service.ejecutar_envio_momento_2(payload=payload)

        # 2. Intento inicial de Momento 3 (Trámite, Fraude o Cierre)
        try:
            return await self._ejecutar_pasos_momento_3(payload, es_fraude=es_fraude, es_cierre=es_cierre)

        except SfcIntegrationException as exc:
            # 🚨 AUTO-RECUPERACIÓN (SELF-HEALING)
            # Se activa si la SFC responde 404 o si la matriz convirtió el mensaje a 'NOT_FOUND_ERROR'
            if exc.status_code == 404 or exc.error_type == "NOT_FOUND_ERROR":
                logger.warning(
                    f"⚠️ [Orquestador] La SFC indica que la queja {smart_code} NO existe en su BD. "
                    f"Iniciando secuencia de auto-recuperación (Momento 2 -> Momento 3)..."
                )

                # Paso A: Crear la queja base en la SFC (Momento 2 + Adjuntos Base)
                logger.info(f"[Auto-Recuperación 1/2] Radicando queja base vía Momento 2 para {smart_code}...")
                res_m2 = await self.m2_service.ejecutar_envio_momento_2(payload=payload)
                
                if res_m2.get("status") != "success":
                    return res_m2

                # Paso B: Reintentar la gestión en Momento 3 (Fraude, Cierre o Trámite)
                logger.info(f"[Auto-Recuperación 2/2] Re-ejecutando pipeline de Momento 3 para {smart_code}...")
                return await self._ejecutar_pasos_momento_3(payload, es_fraude=es_fraude, es_cierre=es_cierre)

            # Si es cualquier otro tipo de error (500, 400 de validación, Auth, etc.), se relanza la excepción
            raise

    async def _ejecutar_pasos_momento_3(
        self, 
        payload: QuejaUnificadaCrmInput, 
        es_fraude: bool, 
        es_cierre: bool
    ) -> Dict[str, Any]:
        """
        Ejecuta secuencialmente los sub-pasos requeridos en Momento 3.
        Si la queja tiene Fraude y Cierre en el mismo payload, transmite ambos en orden regulatorio.
        """
        resultado = {}

        # Sub-paso 1: Reporte / Investigación de Fraude
        if es_fraude:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo gestión de FRAUDE para {payload.Smart_Code__c}...")
            resultado = await self.m3_service.ejecutar_gestion_fraude(payload=payload)

        # Sub-paso 2: Cierre Definitivo
        if es_cierre:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo CIERRE DEFINITIVO para {payload.Smart_Code__c}...")
            resultado = await self.m3_service.ejecutar_cierre_definitivo(payload=payload)

        # Sub-paso 3: Actualización general de Trámite (si no fue ni fraude ni cierre)
        if not es_fraude and not es_cierre:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo ACTUALIZACIÓN DE TRÁMITE para {payload.Smart_Code__c}...")
            resultado = await self.m3_service.ejecutar_actualizacion_tramite(payload=payload)

        return resultado