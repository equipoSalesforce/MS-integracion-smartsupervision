# app/services/momento_2_sync.py
import asyncio
import logging
from typing import Dict, Any, List, Union

from app.integrations.sfc_client import SfcClient
from app.services.s3_service import S3StorageService
from app.schemas.crm_payloads import ArchivoS3Schema, QuejaUnificadaCrmInput, Momento2QuejaCrmInput
from app.schemas.sfc_payloads import SfcNuevaQuejaPayload
from app.core.config import settings
from app.core.mapping import SfcSalesforceMapper
from app.core.exceptions import SfcIntegrationException
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)


class Momento2SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):     
        self.sfc_client = sfc_client
        self.s3_service = S3StorageService(s3_client=s3_client)

    async def ejecutar_envio_momento_2(
        self, 
        payload: Union[QuejaUnificadaCrmInput, Momento2QuejaCrmInput, Dict[str, Any]]
    ) -> Dict[str, Any]:
        if isinstance(payload, dict):
            crm_dict = payload
            smart_code = payload.get("Smart_Code__c")
            archivos_s3_raw = payload.get("archivos_s3", [])
        else:
            crm_dict = payload.model_dump()
            smart_code = payload.Smart_Code__c
            archivos_s3_raw = payload.archivos_s3

        if not smart_code:
            return {"status": "error", "message": "Falta el campo obligatorio 'Smart_Code__c' en el payload."}

        logger.info(f"[Momento 2] Iniciando pipeline de despacho síncrono para el caso: {smart_code}")

        try:
            sfc_raw_payload = SfcSalesforceMapper.crm_entity_to_sfc_payload(crm_dict)
            sfc_id_largo = payload.Smart_Code__c if not isinstance(payload, dict) else smart_code
            sfc_raw_payload["codigo_queja"] = sfc_id_largo

            payload_validado = SfcNuevaQuejaPayload(**sfc_raw_payload)

            logger.info(f"[Momento 2] Enviando queja a la SFC con código regulatorio: {sfc_id_largo}")
            await self.sfc_client.post_nueva_queja(payload_validado.model_dump())
            
            if archivos_s3_raw:
                archivos_schema = [
                    a if isinstance(a, ArchivoS3Schema) else ArchivoS3Schema(**a)
                    for a in archivos_s3_raw
                ]
                await self._procesar_y_enviar_adjuntos_s3(archivos=archivos_schema, sfc_code=sfc_id_largo)

            return {
                "status": "success",
                "message": "Queja y documentos transmitidos correctamente a la SFC de forma síncrona",
                "Smart_Code__c": sfc_id_largo
            }

        except SfcIntegrationException as exc:
            logger.error(f"Error al enviar queja con codigo {smart_code}")
            if getattr(exc, "is_unmapped", False) or getattr(exc, "error_type", None) == "UNKNOWN_ERROR":
                await EmailAlertService.notificar_error_no_mapeado(
                    status_code=getattr(exc, "status_code", 500),
                    raw_message=str(exc),
                    sfc_field=getattr(exc, "sfc_field", None),
                    smart_code=smart_code
                )
            raise

        except Exception as e:
            logger.error(f"Fallo en pipeline del Momento 2 para caso {smart_code}: {str(e)}")
            return {"status": "error", "message": f"Pipeline interrumpido: {str(e)}"}

    async def _procesar_y_enviar_adjuntos_s3(self, archivos: List[ArchivoS3Schema], sfc_code: str):
        """Procesa y envía concurrentemente los anexos del caso utilizando S3StorageService."""
        async def _procesar_un_adjunto(archivo: ArchivoS3Schema):
            s3_key = archivo.s3_key
            if not s3_key:
                return

            file_name = archivo.nombre_archivo or (s3_key.split("/")[-1] if "/" in s3_key else s3_key)
            file_type = file_name.split(".")[-1] if "." in file_name else "pdf"

            # 1. Obtener bytes mediante el servicio unificado de S3
            file_bytes = await self.s3_service.obtener_bytes_archivo(
                s3_key=s3_key, 
                bucket=archivo.bucket
            )

            # 2. Transmitir adjunto a la SFC
            logger.info(f"[Momento 2] Transmitiendo adjunto '{file_name}' a la SFC para caso {sfc_code}.")
            return await self.sfc_client.post_adjunto_queja(
                sfc_codigo_queja=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type,
                file_name=file_name
            )

        tareas = [_procesar_un_adjunto(archivo) for archivo in archivos]
        await asyncio.gather(*tareas)