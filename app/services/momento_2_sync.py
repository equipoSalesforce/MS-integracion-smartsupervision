# app/services/momento_2_sync.py
import asyncio
import logging
from typing import Dict, Any, List, Union
from botocore.exceptions import ClientError

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
        """
        Procesa de forma concurrente los anexos recibidos del CRM, valida su existencia 
        y tamaño en S3, y los transmite a la SFC estandarizando las excepciones de infraestructura.
        """
        if not archivos:
            return

        is_local_env = settings.ENVIRONMENT in ("development", "local")

        # 1. Validación de infraestructura del cliente S3
        if not self.s3_client:
            if is_local_env:
                logger.info("[LOCAL TEST] Generando bytes ficticios locales para simular la carga de adjuntos hacia la SFC.")
            else:
                raise SfcIntegrationException(
                    status_code=500,
                    error_type="INFRASTRUCTURE_ERROR",
                    sfc_field="s3_client",
                    raw_message="El cliente de almacenamiento S3 no está inicializado en el entorno actual.",
                    crm_action="Contactar al equipo de infraestructura/DevOps para validar la configuración de AWS S3."
                )

        async def _procesar_un_adjunto(archivo: ArchivoS3Schema):
            s3_key = archivo.s3_key
            if not s3_key:
                logger.warning("[Momento 2] Se recibió un adjunto sin clave 's3_key', se omitirá.")
                return

            bucket = archivo.bucket or settings.AWS_S3_BUCKET
            file_name = archivo.nombre_archivo or (s3_key.split("/")[-1] if "/" in s3_key else s3_key)
            file_type = file_name.split(".")[-1] if "." in file_name else "pdf"

            # Modo MOCK/DEV sin S3 real
            if is_local_env and not self.s3_client:
                file_bytes = b"Contenido ficticio simulado localmente por el gateway de Global66."
            else:
                # 🎯 A. Captura estandarizada de existencia en S3 / MinIO
                try:
                    metadata = await asyncio.to_thread(
                        self.s3_client.head_object,
                        Bucket=bucket,
                        Key=s3_key
                    )
                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code", "")
                    if error_code in ("404", "403", "NoSuchKey", "NotFound"):
                        logger.error(f"❌ [Momento 2 S3 Error] Archivo '{file_name}' no encontrado (Key: '{s3_key}', Bucket: '{bucket}').")
                        raise SfcIntegrationException(
                            status_code=404,
                            error_type="S3_FILE_NOT_FOUND",
                            sfc_field="archivos_s3",
                            raw_message=f"El archivo '{file_name}' (Key: '{s3_key}') no existe o no se pudo acceder en el almacenamiento S3.",
                            crm_action="Verifique que el archivo haya sido cargado correctamente en el bucket de S3/MinIO antes de reintentar la transmisión."
                        )
                    raise

                # 🎯 B. Captura estandarizada de límite de tamaño (30MB)
                if metadata.get("ContentLength", 0) > 30 * 1024 * 1024:
                    raise SfcIntegrationException(
                        status_code=400,
                        error_type="FILE_SIZE_EXCEEDED",
                        sfc_field="archivos_s3",
                        raw_message=f"El archivo '{file_name}' supera el límite máximo de 30MB permitido por la SFC.",
                        crm_action="Comprima el documento o adjunte una versión de menor tamaño (máximo 30MB)."
                    )

                # 🎯 C. Descarga de bytes en hilo secundario (Non-blocking I/O)
                def _descargar_bytes():
                    s3_file = self.s3_client.get_object(Bucket=bucket, Key=s3_key)
                    return s3_file["Body"].read()

                file_bytes = await asyncio.to_thread(_descargar_bytes)

            # 2. Transmisión a la SFC
            logger.info(f"[Momento 2] Transmitiendo adjunto '{file_name}' a la SFC para queja {sfc_code}.")
            return await self.sfc_client.post_adjunto_queja(
                sfc_codigo_queja=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type,
                file_name=file_name
            )

        # Ejecución paralela de todos los adjuntos del caso
        tareas = [_procesar_un_adjunto(archivo) for archivo in archivos]
        await asyncio.gather(*tareas)