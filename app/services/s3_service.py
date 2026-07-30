import asyncio
import httpx
import logging
from typing import Dict, List, Optional, Any
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.exceptions import SfcIntegrationException

logger = logging.getLogger(__name__)


class S3StorageService:
    """
    Servicio unificado para abstraer operaciones sobre AWS S3 / MinIO.
    Centraliza el manejo asíncrono, validaciones de tamaño, orquestación en lote
    y excepciones estandarizadas.
    """

    def __init__(self, s3_client=None):
        self.s3_client = s3_client
        self.default_bucket = getattr(settings, "AWS_S3_BUCKET", "global66-sfc-bucket-local")
        self.is_local = settings.ENVIRONMENT in ("development", "local")

    @staticmethod
    def normalizar_tipo_archivo(raw_type: str, file_url: str = "") -> tuple[str, str]:
        """
        Normaliza extensiones y tipos MIME para almacenamiento en S3 y envío a SFC.
        """
        val = str(raw_type or "").lower().strip()
        mime_map = {
            "application/pdf": ("pdf", "application/pdf"),
            "pdf": ("pdf", "application/pdf"),
            "image/png": ("png", "image/png"),
            "png": ("png", "image/png"),
            "image/jpeg": ("jpg", "image/jpeg"),
            "jpg": ("jpg", "image/jpeg"),
            "text/plain": ("txt", "text/plain"),
            "txt": ("txt", "text/plain"),
            "application/zip": ("zip", "application/zip"),
            "zip": ("zip", "application/zip")
        }
        if val in mime_map:
            return mime_map[val]
        if "/" in val:
            ext = val.split("/")[-1].replace("vnd.", "").replace("x-", "")
            return ext, val
        if file_url and "." in file_url.split("/")[-1]:
            possible_ext = file_url.split("/")[-1].split(".")[-1].lower()
            if len(possible_ext) <= 4:
                return possible_ext, f"application/{possible_ext}"
        return (val if val else "pdf"), f"application/{val if val else 'pdf'}"

    async def obtener_bytes_archivo(
        self, 
        s3_key: str, 
        bucket: Optional[str] = None, 
        max_size_mb: int = 30
    ) -> bytes:
        """Valida existencia, tamaño y retorna bytes desde S3 de forma asíncrona."""
        target_bucket = bucket or self.default_bucket

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

        try:
            metadata = await asyncio.to_thread(
                self.s3_client.head_object, 
                Bucket=target_bucket, 
                Key=s3_key
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "403", "NoSuchKey", "NotFound"):
                logger.error(f"❌ [S3 Error] Archivo '{file_name}' no encontrado (Key: '{s3_key}').")
                raise SfcIntegrationException(
                    status_code=404,
                    error_type="S3_FILE_NOT_FOUND",
                    sfc_field="archivos_s3",
                    raw_message=f"El archivo '{file_name}' (Key: '{s3_key}') no existe en S3.",
                    crm_action="Verifique que el archivo haya sido cargado correctamente en S3."
                )
            raise

        file_size = metadata.get("ContentLength", 0)
        limit_bytes = max_size_mb * 1024 * 1024
        if file_size > limit_bytes:
            raise SfcIntegrationException(
                status_code=400,
                error_type="FILE_SIZE_EXCEEDED",
                sfc_field="archivos_s3",
                raw_message=f"El archivo '{file_name}' ({file_size / (1024*1024):.2f}MB) supera el límite máximo de {max_size_mb}MB.",
                crm_action=f"Comprima el documento antes de reintentar."
            )

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
        """Sube bytes a S3 de forma asíncrona e inyecta ContentType."""
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
                    crm_action="Contactar al equipo de infraestructura para validar AWS S3."
                )

        try:
            logger.info(f"[S3 Storage] Guardando en Bucket: {target_bucket} | Key: {s3_key}")
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
                raw_message=f"No se pudo guardar la copia en S3: {str(e)}",
                crm_action="Revisar permisos del bucket y conectividad con S3."
            )

    async def listar_archivos_en_directorio(
        self, 
        prefix: str, 
        bucket: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """Escanea un directorio/prefix en S3/MinIO y retorna archivos encontrados."""
        target_bucket = bucket or self.default_bucket
        prefix_clean = prefix.strip()
        if not prefix_clean.endswith("/"):
            prefix_clean += "/"

        if not self.s3_client:
            if self.is_local:
                logger.info(f"[LOCAL S3 MOCK] Listando archivos para prefix: {prefix_clean}")
                return [
                    {"nombre_archivo": "informe_pericial.pdf", "s3_key": f"{prefix_clean}informe_pericial.pdf", "bucket": target_bucket},
                    {"nombre_archivo": "comprobante_pago.pdf", "s3_key": f"{prefix_clean}comprobante_pago.pdf", "bucket": target_bucket}
                ]
            return []

        def _listar():
            response = self.s3_client.list_objects_v2(Bucket=target_bucket, Prefix=prefix_clean)
            objetos = response.get("Contents", [])
            archivos = []
            for obj in objetos:
                key = obj.get("Key", "")
                if key.endswith("/"):
                    continue
                file_name = key.split("/")[-1]
                archivos.append({"nombre_archivo": file_name, "s3_key": key, "bucket": target_bucket})
            return archivos

        try:
            return await asyncio.to_thread(_listar)
        except Exception as e:
            logger.error(f"❌ [S3 Storage] Error listando prefix '{prefix_clean}': {e}")
            return []

    async def transferir_lote_sfc_a_s3(
        self, 
        codigo_queja: str, 
        adjuntos_sfc: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Descarga adjuntos desde la SFC (HTTP) y los respalda en S3. [Momento 1]
        """
        adjuntos_procesados = []

        async with httpx.AsyncClient(timeout=20.0) as http_client:
            for adj in adjuntos_sfc:
                url_sfc = adj.get("file") or adj.get("url") or adj.get("s3_url")
                file_id = adj.get("id")
                raw_type = adj.get("type")

                ext, content_type = self.normalizar_tipo_archivo(raw_type, url_sfc or "")
                filename = f"{file_id}.{ext}" if file_id else f"adjunto_{codigo_queja}.{ext}"
                s3_key = f"quejas/{codigo_queja}/{filename}"

                try:
                    logger.info(f"[S3 Orquestador] Descargando de SFC: {filename}")
                    response = await http_client.get(url_sfc)
                    response.raise_for_status()
                    file_bytes = response.content

                    await self.subir_bytes_archivo(
                        s3_key=s3_key,
                        file_bytes=file_bytes,
                        content_type=content_type
                    )

                    adjuntos_procesados.append({
                        "nombre_archivo": filename,
                        "s3_key": s3_key,
                        "bucket": self.default_bucket
                    })
                except Exception as e:
                    logger.error(f"❌ Error transfiriendo adjunto '{filename}' a S3: {e}")
                    if self.is_local:
                        adjuntos_procesados.append({
                            "nombre_archivo": filename,
                            "s3_key": s3_key,
                            "bucket": self.default_bucket
                        })

        return adjuntos_procesados

    async def transferir_lote_s3_a_sfc(
        self, 
        sfc_client, 
        sfc_codigo_queja: str, 
        adjuntos_crm: List[Any],
        target_file_name: Optional[str] = None,
        afijo_regulatorio: Optional[str] = None,
        afijo_masivo: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Obtiene archivos desde S3 y los transmite a la SFC mediante multipart/form-data.
        Maneja afijos regulatorios y la supresión de duplicados. [Momento 2 y 3]
        """
        resultados = []

        for item in adjuntos_crm:
            s3_key = item.s3_key if hasattr(item, "s3_key") else item.get("s3_key")
            bucket = (item.bucket if hasattr(item, "bucket") else item.get("bucket")) or self.default_bucket
            original_name = (item.nombre_archivo if hasattr(item, "nombre_archivo") else item.get("nombre_archivo")) or (item.get("nombre") if isinstance(item, dict) else None)
            
            if not original_name and s3_key:
                original_name = s3_key.split("/")[-1]
            if not original_name:
                continue

            file_type = original_name.split(".")[-1] if "." in original_name else "pdf"

            try:
                # 1. Obtener bytes desde S3
                file_bytes = item.get("bytes") if isinstance(item, dict) and item.get("bytes") else None
                if not file_bytes:
                    file_bytes = await self.obtener_bytes_archivo(s3_key=s3_key, bucket=bucket)
                    
                # 2. Aplicar lógica de afijos regulatorios si aplica
                debe_aplicar_afijo = afijo_masivo or (target_file_name and original_name == target_file_name)
                if debe_aplicar_afijo and afijo_regulatorio and afijo_regulatorio not in original_name:
                    nombre_puro = original_name.rsplit(".", 1)[0]
                    final_send_name = f"{nombre_puro}_{afijo_regulatorio}.{file_type}"
                    logger.info(f"[S3 Orquestador] Inyectando afijo '{afijo_regulatorio}': '{original_name}' -> '{final_send_name}'")
                else:
                    final_send_name = original_name

                # 3. Transmitir adjunto a la SFC
                res_sfc = await sfc_client.post_adjunto_queja(
                    sfc_codigo_queja=sfc_codigo_queja,
                    file_bytes=file_bytes,
                    file_type=file_type,
                    file_name=final_send_name
                )
                resultados.append({"file_name": final_send_name, "status": "OK", "sfc_response": res_sfc})
                logger.info(f"✅ Adjunto '{final_send_name}' transmitido exitosamente a la SFC.")

            except SfcIntegrationException as exc:
                raw_msg = (getattr(exc, "raw_message", "") or str(exc)).lower()
                if getattr(exc, "error_type", None) == "DUPLICATE_FILE" or "ya existe" in raw_msg or "556240" in raw_msg:
                    logger.warning(f"⚠️ Archivo '{original_name}' duplicado en SFC. Se omite de forma segura.")
                    resultados.append({"file_name": original_name, "status": "DUPLICATE_OMITTED"})
                else:
                    raise
            except Exception as e:
                logger.error(f"❌ Error al transferir '{original_name}' a la SFC: {e}")
                raise

        return resultados