# app/services/s3_service.py
import asyncio
import logging
from typing import Optional
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.exceptions import SfcIntegrationException

logger = logging.getLogger(__name__)


class S3StorageService:
    """
    Servicio unificado para abstraer operaciones sobre AWS S3 / MinIO.
    Centraliza el manejo asíncrono, validaciones de tamaño y excepciones estandarizadas.
    """

    def __init__(self, s3_client=None):
        self.s3_client = s3_client
        self.default_bucket = getattr(settings, "AWS_S3_BUCKET", "global66-sfc-bucket-local")
        self.is_local = settings.ENVIRONMENT in ("development", "local")

    async def obtener_bytes_archivo(
        self, 
        s3_key: str, 
        bucket: Optional[str] = None, 
        max_size_mb: int = 30
    ) -> bytes:
        """
        Valida la existencia del archivo, valida su tamaño y retorna sus bytes de forma asíncrona.
        """
        target_bucket = bucket or self.default_bucket

        # Fallback para desarrollo local si no hay cliente S3
        if not self.s3_client:
            if self.is_local:
                logger.info(f"[LOCAL S3 MOCK] Generando bytes simulados para key: {s3_key}")
                return b"Contenido ficticio simulado localmente por el gateway de Global66."
            else:
                raise SfcIntegrationException(
                    status_code=500,
                    error_type="INFRASTRUCTURE_ERROR",
                    sfc_field="s3_client",
                    raw_message="El cliente de almacenamiento S3 no está inicializado en producción.",
                    crm_action="Contactar al equipo de infraestructura para validar la configuración de AWS S3."
                )

        file_name = s3_key.split("/")[-1] if "/" in s3_key else s3_key

        # 1. Validar existencia y permisos en S3
        try:
            metadata = await asyncio.to_thread(
                self.s3_client.head_object, 
                Bucket=target_bucket, 
                Key=s3_key
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "403", "NoSuchKey", "NotFound"):
                logger.error(f"❌ [S3 Error] Archivo '{file_name}' no encontrado (Key: '{s3_key}', Bucket: '{target_bucket}').")
                raise SfcIntegrationException(
                    status_code=404,
                    error_type="S3_FILE_NOT_FOUND",
                    sfc_field="archivos_s3",
                    raw_message=f"El archivo '{file_name}' (Key: '{s3_key}') no existe o no se pudo acceder en el almacenamiento S3.",
                    crm_action="Verifique que el archivo haya sido cargado correctamente en el bucket de S3/MinIO antes de reintentar."
                )
            raise

        # 2. Validar tamaño máximo
        file_size = metadata.get("ContentLength", 0)
        limit_bytes = max_size_mb * 1024 * 1024
        if file_size > limit_bytes:
            raise SfcIntegrationException(
                status_code=400,
                error_type="FILE_SIZE_EXCEEDED",
                sfc_field="archivos_s3",
                raw_message=f"El archivo '{file_name}' ({file_size / (1024*1024):.2f}MB) supera el límite máximo de {max_size_mb}MB permitido por la SFC.",
                crm_action=f"Comprima el documento o adjunte una versión de menor tamaño (máximo {max_size_mb}MB)."
            )

        # 3. Descargar bytes en hilo secundario (Non-blocking I/O)
        def _descargar():
            s3_file = self.s3_client.get_object(Bucket=target_bucket, Key=s3_key)
            return s3_file["Body"].read()

        return await asyncio.to_thread(_descargar)

    async def subir_bytes_archivo(
        self, 
        s3_key: str, 
        file_bytes: bytes, 
        content_type: str = "application/pdf", 
        bucket: Optional[str] = None
    ) -> str:
        """
        Sube un arreglo de bytes a S3 de forma asíncrona inyectando ContentType.
        Retorna la clave s3_key.
        """
        target_bucket = bucket or self.default_bucket

        if not self.s3_client:
            if self.is_local:
                logger.info(f"[LOCAL S3 MOCK] Subida simulada para key: {s3_key}")
                return s3_key
            else:
                raise SfcIntegrationException(
                    status_code=500,
                    error_type="INFRASTRUCTURE_ERROR",
                    sfc_field="s3_client",
                    raw_message="El cliente S3 no está inicializado en producción.",
                    crm_action="Contactar al equipo de infraestructura para validar la configuración de AWS S3."
                )

        try:
            logger.info(f"[S3 Storage] Guardando en Bucket: {target_bucket} | Key: {s3_key} | ContentType: {content_type}")
            await asyncio.to_thread(
                self.s3_client.put_object,
                Bucket=target_bucket,
                Key=s3_key,
                Body=file_bytes,
                ContentType=content_type
            )
            return s3_key
        except Exception as e:
            logger.error(f"❌ [S3 Storage] Error al subir archivo {s3_key}: {str(e)}")
            raise SfcIntegrationException(
                status_code=500,
                error_type="S3_UPLOAD_ERROR",
                sfc_field="archivos_s3",
                raw_message=f"No se pudo guardar la copia del archivo en S3: {str(e)}",
                crm_action="Revisar permisos del bucket y conectividad con el servicio de almacenamiento."
            )