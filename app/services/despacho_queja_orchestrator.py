# app/services/despacho_queja_orchestrator.py
import logging
from typing import Dict, Any, Optional
from pydantic import ValidationError

from app.integrations.sfc_client import SfcClient
from app.schemas.crm_payloads import ArchivoS3Schema, QuejaUnificadaCrmInput
from app.services.momento_2_sync import Momento2SincronizacionService
from app.services.momento_3_sync import Momento3SincronizacionService
from app.services.email_service import EmailAlertService
from app.core.exceptions import SfcIntegrationException

logger = logging.getLogger(__name__)


def _es_error_caso_ya_cerrado(exc: Exception) -> bool:
    """
    Evalúa si la SFC rechazó la petición porque el registro de la queja
    ya cuenta con un documento de respuesta final o se encuentra en estado (4) Cerrado.
    """
    raw_msg = (getattr(exc, "raw_message", "") or str(exc)).lower()
    keywords = [
        "ya cuenta con un documento de respuesta final",
        "diferente de (4) cerrado",
        "respuesta final"
    ]
    return any(kw in raw_msg for kw in keywords)


class DespachoQuejaOrquestador:
    """
    Servicio Stateless que actúa como fachada/orquestador único para la transmisión 
    de quejas desde Salesforce hacia la SFC (Momento 2 + Momento 3).
    Incluye lógica de inferencia automática y auto-recuperación (Self-Healing).
    """

    def __init__(
        self, 
        sfc_client: SfcClient, 
        s3_client=None,
        m2_service: Optional[Momento2SincronizacionService] = None,
        m3_service: Optional[Momento3SincronizacionService] = None
    ):
        self.sfc_client = sfc_client
        self.s3_client = s3_client
        # 🎯 Inyección de Dependencias con Fallback por defecto:
        # Si se pasan instancias (ej. Mocks en tests), usa esas; si vienen en None (en producción),
        # instancia los servicios reales usando sfc_client y s3_client.
        self.m2_service = m2_service or Momento2SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
        self.m3_service = m3_service or Momento3SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)

    async def procesar_despacho_raw_json(self, payload_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Rehidrata un diccionario/JSON desde Redis al esquema Pydantic 'QuejaUnificadaCrmInput'."""
        try:
            payload = QuejaUnificadaCrmInput.model_validate(payload_dict)
            return await self.procesar_despacho(payload=payload)
        except ValidationError as ve:
            logger.error(f"[Orquestador] Error de validación Pydantic al rehidratar desde la cola Redis: {ve.json()}")
            raise Exception(f"Estructura inválida en el payload rehidratado de Redis: {str(ve)}")

    async def procesar_despacho(self, payload: QuejaUnificadaCrmInput) -> Dict[str, Any]:
        smart_code = payload.Smart_Code__c
        status_raw = (payload.Status or "").strip().lower()

        # Inspección y listado dinámico desde S3 si viene solo la ruta del directorio
        if payload.directorio_s3 and not payload.archivos_s3:
            s3_service = self.m3_service.s3_service
            archivos_remotos = await s3_service.listar_archivos_en_directorio(prefix=payload.directorio_s3)
            
            if archivos_remotos:
                logger.info(f"📂 [Orquestador] Encontrados {len(archivos_remotos)} archivos en directorio '{payload.directorio_s3}'")
                payload.archivos_s3 = [
                    ArchivoS3Schema(**a) for a in archivos_remotos
                ]
            else:
                logger.warning(f"⚠️ [Orquestador] No se encontraron archivos en el directorio S3 '{payload.directorio_s3}'")    
        
        es_cierre = (
            status_raw in ("closed", "cerrado") or 
            payload.ClosedDate is not None or 
            payload.Favorabilidad__c is not None
        )
        es_fraude = (
            payload.tipo_fraude__c is not None or 
            payload.modalidad_fraude__c is not None
        )
        
        if es_fraude and not payload.archivos_s3:
            raise SfcIntegrationException(
                status_code=400,
                error_type="CRM_PAYLOAD_VALIDATION_ERROR",
                sfc_field="archivos_s3",
                raw_message="No se encontraron documentos de investigación de fraude (INV_FRAUDE_SFC) en S3.",
                crm_action="Asegúrese de cargar los documentos de soporte de la investigación de fraude en S3 antes de enviar el caso."
            )
        
        tiene_campos_m3 = any([
            payload.sc_genero__c is not None,
            payload.sc_LGBTIQ__c is not None,
            payload.sc_Condicion_especial__c is not None,
            payload.producto_digital__c is not None,
            payload.admision_col__c != "No Aplica"
        ])
        
        es_m2_puro = (status_raw in ("new", "nuevo")) and not tiene_campos_m3 and not es_fraude and not es_cierre

        logger.info(
            f"[Orquestador] Procesando solicitud para el caso {smart_code} "
            f"(Es Cierre: {es_cierre}, Es Fraude: {es_fraude}, Es M2 Puro: {es_m2_puro})"
        )

        try:
            # 1. Caso de Alta Nueva Puro (Creación inicial vía Momento 2)
            if es_m2_puro:
                logger.info(f"[Orquestador] Ejecutando despacho directo de QUEJA NUEVA (M2) para {smart_code}")
                return await self.m2_service.ejecutar_envio_momento_2(payload=payload)

            # 2. Intento de Momento 3 (Trámite, Fraude o Cierre)
            # Si el caso no existe en la SFC, saltará el 404 y el bloque except ejecutará el Self-Healing (M2 -> M3)
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

                # Paso A: Crear la queja base vía Momento 2 OMITIENDO adjuntos para que M3 los transmita con afijo
                logger.info(f"[Auto-Recuperación 1/2] Radicando queja base vía Momento 2 para {smart_code}...")
                
                payload_m2_sin_adjuntos = payload.model_copy()
                payload_m2_sin_adjuntos.archivos_s3 = []
                payload_m2_sin_adjuntos.directorio_s3 = None
                
                res_m2 = await self.m2_service.ejecutar_envio_momento_2(payload=payload_m2_sin_adjuntos)

                if res_m2.get("status") != "success":
                    return res_m2

                # Paso B: Aplicar la actualización de Momento 3 (procesa adjuntos con sus afijos normativos)
                logger.info(f"[Auto-Recuperación 2/2] Re-ejecutando pipeline de Momento 3 para {smart_code}...")
                return await self._ejecutar_pasos_momento_3(payload, es_fraude=es_fraude, es_cierre=es_cierre)
            
            # ⚠️ EVALUACIÓN DE ERROR NO MAPEADO
            if is_unmapped:
                logger.warning(f"⚠️ [Orquestador] Se detectó un error no mapeado en SFC para el caso {smart_code}.")
                raise 

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
            try:
                resultado = await self.m3_service.ejecutar_gestion_fraude(payload=payload)
                if isinstance(resultado, dict) and resultado.get("status") == "error":
                    return resultado
            except SfcIntegrationException as exc:
                # 🛡️ CAPTURA DE ERROR SI EL CASO YA TIENE RESPUESTA FINAL Y VIENE UN CIERRE
                if es_cierre and _es_error_caso_ya_cerrado(exc):
                    logger.warning(
                        f"⚠️ [Orquestador] El caso {payload.Smart_Code__c} ya cuenta con respuesta final/está cerrado en SFC. "
                        f"Omitiendo la falla intermedia de Fraude y procediendo con el Cierre..."
                    )
                else:
                    raise

        if es_cierre:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo CIERRE DEFINITIVO para {payload.Smart_Code__c}...")
            try:
                resultado = await self.m3_service.ejecutar_cierre_definitivo(payload=payload)
            except SfcIntegrationException as exc:
                # 🛡️ SI LA PROPIA LLAMADA DE CIERRE RECHAZA PORQUE YA FIGURA COMO CERRADO (ESTADO 4)
                if _es_error_caso_ya_cerrado(exc):
                    logger.info(
                        f"✅ [Orquestador] El caso {payload.Smart_Code__c} ya figuraba como cerrado con respuesta final en SFC. "
                        f"Marcando la operación como exitosa."
                    )
                    return {
                        "status": "success",
                        "message": f"Caso {payload.Smart_Code__c} ya se encuentra cerrado en la SFC (Estado 4).",
                        "codigo_queja_sfc": payload.Smart_Code__c
                    }
                raise

        if not es_fraude and not es_cierre:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo ACTUALIZACIÓN DE TRÁMITE para {payload.Smart_Code__c}...")
            resultado = await self.m3_service.ejecutar_actualizacion_tramite(payload=payload)

        return resultado