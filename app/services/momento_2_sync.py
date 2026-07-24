# app/services/momento_2_sync.py
import asyncio
import logging
from typing import Dict, Any, List, Union

from app.integrations.sfc_client import SfcClient
from app.schemas.crm_payloads import (
    ArchivoS3Schema, 
    QuejaUnificadaCrmInput, 
    Momento2QuejaCrmInput
)
from app.schemas.sfc_payloads import SfcNuevaQuejaPayload
from app.core.config import settings
from app.core.mapping import SfcSalesforceMapper
from app.core.exceptions import SfcIntegrationException
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)


class Momento2SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):     
        self.sfc_client = sfc_client
        self.s3_client = s3_client

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
            
            logger.info(f"[Momento 2] Recibidos {len(archivos_s3_raw)} archivos para enviar.")
            
            if payload_validado.anexo_queja and not archivos_s3_raw:
                logger.warning(
                    f"[Momento 2] La queja {smart_code} marca 'anexo_queja' como Verdadero, "
                    f"pero la lista 'archivos_s3' llegó vacía desde el CRM."
                )

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
            
            if settings.ENVIRONMENT == "local":
                logger.info(sfc_raw_payload)
                        
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
        if not self.s3_client:
            if settings.ENVIRONMENT == "development" or settings.ENVIRONMENT == "local":
                logger.info("[LOCAL TEST] Generando bytes ficticios locales para simular la carga de adjuntos hacia la SFC.")
                tareas_envio = []

                for archivo in archivos:
                    s3_key = archivo.s3_key
                    if not s3_key:
                        logger.warning("[Momento 2] Se recibió un adjunto sin clave 's3_key' en modo local, se omitirá.")
                        continue
                    
                    file_bytes = b"Contenido ficticio simulado localmente por el gateway de Global66."
                    file_type = s3_key.split(".")[-1] if "." in s3_key else "pdf"
                    file_name = archivo.nombre_archivo or (s3_key.split("/")[-1] if "/" in s3_key else s3_key)

                    tareas_envio.append(self.sfc_client.post_adjunto_queja(
                        sfc_codigo_queja=sfc_code,
                        file_bytes=file_bytes,
                        file_type=file_type,
                        file_name=file_name
                    ))

                if tareas_envio:
                    logger.info(f"[LOCAL TEST] Transmitiendo de forma concurrente {len(tareas_envio)} anexos simulados al Mock SFC.")
                    await asyncio.gather(*tareas_envio)
                
                return
            else:
                raise ValueError("Error de infraestructura: El cliente S3 no está inicializado en producción.")

        tareas_envio = []

        for archivo in archivos:
            s3_key = archivo.s3_key
            bucket = archivo.bucket or settings.AWS_S3_BUCKET
            
            if not s3_key:
                logger.warning("[Momento 2] Se recibió un adjunto sin clave 's3_key', se omitirá.")
                continue

            metadata = await asyncio.to_thread(
                self.s3_client.head_object,
                Bucket=bucket,
                Key=s3_key
            )
            
            file_size = metadata.get("ContentLength", 0)

            if file_size > 30 * 1024 * 1024:
                raise ValueError(f"El archivo {s3_key} supera el límite de 30MB permitido por la SFC.")

            s3_file = await asyncio.to_thread(
                self.s3_client.get_object,
                Bucket=bucket,
                Key=s3_key
            )
            file_bytes = s3_file["Body"].read()
            file_type = s3_key.split(".")[-1] if "." in s3_key else "pdf"
            file_name = archivo.nombre_archivo or (s3_key.split("/")[-1] if "/" in s3_key else s3_key)
            
            tareas_envio.append(self.sfc_client.post_adjunto_queja(
                sfc_codigo_queja=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type,
                file_name=file_name
            ))

        if tareas_envio:
            logger.info(f"[Momento 2] Transmitiendo concurrentemente {len(tareas_envio)} anexos de S3 a la SFC.")
            await asyncio.gather(*tareas_envio)