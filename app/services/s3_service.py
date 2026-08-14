import asyncio
import io
import logging
import tempfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from botocore.exceptions import ClientError, BotoCoreError
import httpx

from app.core.config import settings
from app.core.exceptions import SfcIntegrationException
from app.db.redis import get_redis_client
from app.services.idempotency_service import IdempotencyService

logger = logging.getLogger(__name__)


class S3StorageService:
    """
    Servicio unificado para abstraer operaciones sobre AWS S3 / MinIO.
    Centraliza el manejo asíncrono, validaciones de tamaño e integridad,
    orquestación en lote y excepciones estandarizadas utilizando Streaming
    para prevenir errores de falta de memoria (OOM).
    """

    def __init__(self, s3_client=None, http_client: Optional[httpx.AsyncClient] = None):
        self.s3_client = s3_client
        self.default_bucket = settings.AWS_S3_BUCKET
        self.is_local = settings.ENVIRONMENT in ("development", "local")
        self.http_client = http_client

    @staticmethod
    def _limpiar_key(key_or_prefix: str) -> str:
        """Remueve barras inclinadas iniciales para evitar claves inconsistentes en S3."""
        if not key_or_prefix:
            return ""
        return key_or_prefix.strip().lstrip("/")

    @staticmethod
    def normalizar_tipo_archivo(raw_type: str, file_url: str = "") -> Tuple[str, str]:
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

    @staticmethod
    def validar_integridad_archivo(file_data: Any, file_name: str):
        ext = file_name.split(".")[-1].lower().strip() if "." in file_name else ""

        if isinstance(file_data, bytes):
            if not file_data or len(file_data) == 0:
                raise SfcIntegrationException(
                    status_code=400,
                    error_type="FILE_EMPTY_ERROR",
                    sfc_field="archivos_s3",
                    raw_message=f"El archivo '{file_name}' está completamente vacío (0 bytes).",
                    crm_action="Verifique y cargue un archivo válido con contenido en S3 antes de reintentar."
                )
            header = file_data[:1024]
        else:
            file_data.seek(0, 2)
            size = file_data.tell()
            file_data.seek(0)
            
            if size == 0:
                raise SfcIntegrationException(
                    status_code=400,
                    error_type="FILE_EMPTY_ERROR",
                    sfc_field="archivos_s3",
                    raw_message=f"El archivo '{file_name}' está completamente vacío (0 bytes).",
                    crm_action="Verifique y cargue un archivo válido con contenido en S3 antes de reintentar."
                )
            header = file_data.read(1024)
            file_data.seek(0)

        magic_headers = {
            "pdf": [b"%PDF-"],
            "png": [b"\x89PNG\r\n\x1a\n"],
            "jpg": [b"\xff\xd8\xff"],
            "jpeg": [b"\xff\xd8\xff"],
            "zip": [b"PK\x03\x04"],
        }

        if ext in magic_headers:
            signatures = magic_headers[ext]
            if not any(header.startswith(sig) for sig in signatures):
                logger.error(f"❌ [S3 Integrity] Archivo '{file_name}' no coincide con los Magic Bytes de tipo {ext.upper()}.")
                raise SfcIntegrationException(
                    status_code=400,
                    error_type="CORRUPTED_OR_INVALID_FILE",
                    sfc_field="archivos_s3",
                    raw_message=f"El archivo '{file_name}' está corrupto o su contenido no corresponde a un formato {ext.upper()} válido.",
                    crm_action="Verifique la integridad y formato real del archivo antes de subirlo a S3."
                )

    def _es_host_permitido_sfc(self, url: str) -> bool:
        if not url:
            return False
        try:
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            if not hostname:
                return False

            sfc_base_host = (urlparse(settings.SFC_URL_BASE).hostname or "").lower()
            if sfc_base_host and (hostname == sfc_base_host or hostname.endswith("." + sfc_base_host)):
                return True

            allowed_domains = (
                "superfinanciera.gov.co",
                "storage.googleapis.com",
                "amazonaws.com",
                "cloud.goog"
            )
            if any(hostname == domain or hostname.endswith("." + domain) for domain in allowed_domains):
                return True

            if self.is_local or settings.ENVIRONMENT in ("local", "development", "qa"):
                if hostname in ("localhost", "127.0.0.1", "minio", "mock-sfc", "unscanned"):
                    return True

            return False
        except Exception:
            return False
    
    async def obtener_stream_archivo(
        self,
        s3_key: str,
        bucket: Optional[str] = None,
        max_size_mb: int = 30,
        case_id_esperado: Optional[str] = None
    ) -> tempfile.SpooledTemporaryFile:
        s3_key_clean = self._limpiar_key(s3_key)
        target_bucket = self.default_bucket #Retirado bucket opcional para evitar inyecciones
        file_name = s3_key_clean.split("/")[-1] if "/" in s3_key_clean else s3_key_clean

        # 🟢 FIX P1-10: el bucket ya está fijado server-side, pero la key en sí seguía
        # siendo aceptada sin validar que perteneciera al caso que se está procesando —
        # un consumidor autenticado podía referenciar (adrede o por error) un archivo de
        # OTRO caso dentro del mismo bucket. Se valida contra Case_id (no Smart_Code__c):
        # es el identificador real y estable dentro del CRM/DB — Smart_Code__c puede
        # derivarse/generarse desde el schema cuando sólo llega Case_id, por lo que no es
        # confiable para esta comparación en todos los casos (ej. recuperados en M1).
        if case_id_esperado:
            segmentos = [seg for seg in s3_key_clean.split("/") if seg]
            if case_id_esperado not in segmentos:
                logger.error(
                    f"🚨 [S3 Ownership] La key '{s3_key_clean}' no pertenece al caso "
                    f"'{case_id_esperado}' que se está procesando."
                )
                raise SfcIntegrationException(
                    status_code=403,
                    error_type="S3_KEY_OWNERSHIP_MISMATCH",
                    sfc_field="s3_key",
                    raw_message=f"La key '{s3_key_clean}' no corresponde al caso '{case_id_esperado}'.",
                    crm_action="Verifique que los archivos referenciados (s3_key) pertenezcan al caso que se está enviando."
                )

        tmp_file = tempfile.SpooledTemporaryFile(max_size=5 * 1024 * 1024)

        try:
            if not self.s3_client:
                if self.is_local:
                    logger.info(f"[LOCAL S3 MOCK] Generando stream simulado para key: {s3_key_clean}")
                    ext = file_name.split(".")[-1].lower() if "." in file_name else "pdf"
                    mock_headers = {
                        "pdf": b"%PDF-1.4 Mock PDF content for local testing",
                        "png": b"\x89PNG\r\n\x1a\nMock PNG content",
                        "jpg": b"\xff\xd8\xffMock JPG content",
                        "jpeg": b"\xff\xd8\xffMock JPEG content",
                        "zip": b"PK\x03\x04Mock ZIP content"
                    }
                    tmp_file.write(mock_headers.get(ext, b"%PDF-1.4 Mock content default"))
                    tmp_file.seek(0)
                    return tmp_file
                else:
                    raise SfcIntegrationException(
                        status_code=500,
                        error_type="INFRASTRUCTURE_ERROR",
                        sfc_field="s3_client",
                        raw_message="El cliente de almacenamiento S3 no está inicializado en producción.",
                        crm_action="Contactar al equipo de infraestructura para validar la configuración de AWS S3."
                    )

            try:
                metadata = await asyncio.to_thread(
                    self.s3_client.head_object, 
                    Bucket=target_bucket, 
                    Key=s3_key_clean
                )
            except ClientError as e:
                error_code = str(e.response.get("Error", {}).get("Code", ""))
                # 🟢 FIX HALLAZGO 15: Se separa 403 (AccessDenied) de los errores 404 (NotFound)
                if error_code in ("404", "NoSuchKey", "NotFound"):
                    logger.error(f"❌ [S3 Error] Archivo '{file_name}' no encontrado (Key: '{s3_key_clean}').")
                    raise SfcIntegrationException(
                        status_code=404,
                        error_type="S3_FILE_NOT_FOUND",
                        sfc_field="archivos_s3",
                        raw_message=f"El archivo '{file_name}' (Key: '{s3_key_clean}') no existe en S3.",
                        crm_action="Verifique que el archivo haya sido cargado correctamente en S3."
                    )
                elif error_code in ("403", "AccessDenied"):
                    logger.error(f"🚨 [S3 Error] Acceso denegado a Key '{s3_key_clean}' en bucket '{target_bucket}'.")
                    raise SfcIntegrationException(
                        status_code=500,
                        error_type="S3_ACCESS_DENIED",
                        sfc_field="archivos_s3",
                        raw_message=f"Acceso denegado al leer el archivo en S3 (Key: '{s3_key_clean}').",
                        crm_action="Verifique los permisos IAM del rol ECS sobre la política del bucket S3."
                    )
                raise SfcIntegrationException(
                    status_code=500,
                    error_type="S3_INFRASTRUCTURE_ERROR",
                    sfc_field="archivos_s3",
                    raw_message=f"Fallo de infraestructura en S3 ({error_code}): {str(e)}",
                    crm_action="Revisar conectividad y estado del servicio de AWS S3."
                )

            file_size = metadata.get("ContentLength", 0)
            
            if file_size == 0:
                raise SfcIntegrationException(
                    status_code=400,
                    error_type="FILE_EMPTY_ERROR",
                    sfc_field="archivos_s3",
                    raw_message=f"El archivo '{file_name}' está completamente vacío (0 bytes).",
                    crm_action="Cargue un archivo válido con contenido en S3 antes de reintentar."
                )

            limit_bytes = max_size_mb * 1024 * 1024
            if file_size > limit_bytes:
                raise SfcIntegrationException(
                    status_code=400,
                    error_type="FILE_SIZE_EXCEEDED",
                    sfc_field="archivos_s3",
                    raw_message=f"El archivo '{file_name}' ({file_size / (1024*1024):.2f}MB) supera el límite máximo de {max_size_mb}MB.",
                    crm_action="Comprima el documento antes de reintentar."
                )

            def _descargar():
                self.s3_client.download_fileobj(Bucket=target_bucket, Key=s3_key_clean, Fileobj=tmp_file)

                if tmp_file.tell() == 0 and hasattr(self.s3_client, "get_object"):
                    try:
                        res = self.s3_client.get_object(Bucket=target_bucket, Key=s3_key_clean)
                        if isinstance(res, dict) and "Body" in res:
                            body = res["Body"]
                            content = body.read() if hasattr(body, "read") and callable(body.read) else body
                            if isinstance(content, bytes) and len(content) > 0:
                                tmp_file.write(content)
                    except Exception:
                        pass

            await asyncio.to_thread(_descargar)
            tmp_file.seek(0)

            self.validar_integridad_archivo(file_data=tmp_file, file_name=file_name)
            return tmp_file

        except Exception as e:
            tmp_file.close()
            raise e

    async def obtener_bytes_archivo(
        self, 
        s3_key: str, 
        bucket: Optional[str] = None, 
        max_size_mb: int = 30
    ) -> bytes:
        tmp_file = await self.obtener_stream_archivo(s3_key, bucket, max_size_mb)
        try:
            return tmp_file.read()
        finally:
            tmp_file.close()

    async def subir_stream_archivo(
        self, 
        s3_key: str, 
        file_obj: Any, 
        content_type: str = "application/pdf", 
        bucket: Optional[str] = None
    ) -> str:
        s3_key_clean = self._limpiar_key(s3_key)
        target_bucket = bucket or self.default_bucket

        if not self.s3_client:
            if self.is_local:
                logger.info(f"[LOCAL S3 MOCK] Subida simulada (Stream) para key: {s3_key_clean}")
                return s3_key_clean
            else:
                raise SfcIntegrationException(
                    status_code=500,
                    error_type="INFRASTRUCTURE_ERROR",
                    sfc_field="s3_client",
                    raw_message="El cliente S3 no está inicializado en producción.",
                    crm_action="Contactar al equipo de infraestructura para validar AWS S3."
                )

        try:
            logger.info(f"[S3 Storage] Subiendo stream a Bucket: {target_bucket} | Key: {s3_key_clean}")
            file_obj.seek(0)
            
            await asyncio.to_thread(
                self.s3_client.upload_fileobj,
                Fileobj=file_obj,
                Bucket=target_bucket,
                Key=s3_key_clean,
                ExtraArgs={'ContentType': content_type}
            )
            return s3_key_clean
        except Exception as e:
            logger.error(f"❌ [S3 Storage] Error al subir stream {s3_key_clean}: {str(e)}")
            raise SfcIntegrationException(
                status_code=500,
                error_type="S3_UPLOAD_ERROR",
                sfc_field="archivos_s3",
                raw_message=f"No se pudo guardar la copia en S3: {str(e)}",
                crm_action="Revisar permisos del bucket y conectividad con S3."
            )

    async def subir_bytes_archivo(
        self, 
        s3_key: str, 
        file_bytes: bytes, 
        content_type: str = "application/pdf", 
        bucket: Optional[str] = None
    ) -> str:
        s3_key_clean = self._limpiar_key(s3_key)
        target_bucket = bucket or self.default_bucket

        if not self.s3_client:
            if self.is_local:
                logger.info(f"[LOCAL S3 MOCK] Subida simulada para key: {s3_key_clean}")
                return s3_key_clean
            else:
                raise SfcIntegrationException(
                    status_code=500,
                    error_type="INFRASTRUCTURE_ERROR",
                    sfc_field="s3_client",
                    raw_message="El cliente S3 no está inicializado en producción.",
                    crm_action="Contactar al equipo de infraestructura para validar AWS S3."
                )

        try:
            logger.info(f"[S3 Storage] Guardando en Bucket: {target_bucket} | Key: {s3_key_clean}")
            await asyncio.to_thread(
                self.s3_client.put_object,
                Bucket=target_bucket,
                Key=s3_key_clean,
                Body=file_bytes,
                ContentType=content_type
            )
            return s3_key_clean
        except Exception as e:
            logger.error(f"❌ [S3 Storage] Error al subir archivo {s3_key_clean}: {str(e)}")
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
        target_bucket = bucket or self.default_bucket
        prefix_clean = self._limpiar_key(prefix)
        if prefix_clean and not prefix_clean.endswith("/"):
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
            paginator = self.s3_client.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(Bucket=target_bucket, Prefix=prefix_clean)
            
            archivos = []
            for page in page_iterator:
                objetos = page.get("Contents", [])
                for obj in objetos:
                    key = obj.get("Key", "")
                    if key.endswith("/"):
                        continue
                    file_name = key.split("/")[-1]
                    archivos.append({
                        "nombre_archivo": file_name, 
                        "s3_key": key, 
                        "bucket": target_bucket
                    })
            return archivos

        try:
            return await asyncio.to_thread(_listar)
        except ClientError as e:
            error_code = str(e.response.get("Error", {}).get("Code", ""))
            
            # 🟢 FIX HALLAZGO 15: Diferenciar inactividad de directorio (404) vs fallos de permisos / red (500)
            if error_code in ("NoSuchBucket", "NoSuchKey", "NotFound", "404"):
                logger.warning(f"⚠️ [S3 Storage] Directorio/Bucket no encontrado para prefix '{prefix_clean}': {error_code}")
                return []
            
            logger.error(f"❌ [S3 Storage] Fallo de permisos/infraestructura S3 listando prefix '{prefix_clean}' ({error_code}): {e}")
            raise SfcIntegrationException(
                status_code=500,
                error_type="S3_LIST_ERROR",
                sfc_field="archivos_s3",
                raw_message=f"Error de infraestructura/permisos S3 al listar '{prefix_clean}': {error_code} - {str(e)}",
                crm_action="Verificar conectividad S3 y permisos IAM (s3:ListBucket) sobre el bucket."
            )
        except SfcIntegrationException:
            raise
        except Exception as e:
            logger.error(f"❌ [S3 Storage] Error inesperado listando prefix '{prefix_clean}': {e}")
            raise SfcIntegrationException(
                status_code=500,
                error_type="S3_LIST_ERROR",
                sfc_field="archivos_s3",
                raw_message=f"Error inesperado de almacenamiento S3 al listar '{prefix_clean}': {str(e)}",
                crm_action="Revisar conectividad y logs de infraestructura S3."
            )

    async def transferir_lote_sfc_a_s3(
        self, 
        codigo_queja: str, 
        adjuntos_sfc: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if not adjuntos_sfc:
            return []

        sem = asyncio.Semaphore(5)

        async def _procesar_adjunto(client: httpx.AsyncClient, adj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            async with sem:
                url_sfc = adj.get("file") or adj.get("url") or adj.get("s3_url")
                file_id = adj.get("id")
                raw_type = adj.get("type")

                ext, content_type = self.normalizar_tipo_archivo(raw_type, url_sfc or "")
                filename = f"{file_id}.{ext}" if file_id else f"adjunto_{codigo_queja}.{ext}"
                s3_key = f"quejas/{codigo_queja}/{filename}"

                tmp_file = tempfile.SpooledTemporaryFile(max_size=5 * 1024 * 1024)

                try:
                    if not self._es_host_permitido_sfc(url_sfc):
                        logger.error(f"🚨 [SSRF Protection] Bloqueada descarga de URL no autorizada: '{url_sfc}'")
                        raise SfcIntegrationException(
                            status_code=400,
                            error_type="SSRF_PROTECTION_ERROR",
                            sfc_field="archivos_s3",
                            raw_message=f"La URL de descarga de adjunto '{url_sfc}' no pertenece a un dominio permitido por la SFC.",
                            crm_action="Verifique la URL del archivo adjunto provista por la SFC."
                        )

                    logger.info(f"[S3 Orquestador] Descargando de SFC con Streaming (Límite 30MB): {filename}")

                    max_bytes = 30 * 1024 * 1024
                    downloaded_bytes = 0
                    timeout_descarga = httpx.Timeout(30.0, connect=5.0)

                    async with client.stream("GET", url_sfc, timeout=timeout_descarga) as response:
                        response.raise_for_status()

                        content_length = response.headers.get("Content-Length")
                        if content_length and content_length.isdigit():
                            if int(content_length) > max_bytes:
                                raise SfcIntegrationException(
                                    status_code=400,
                                    error_type="FILE_SIZE_EXCEEDED",
                                    sfc_field="archivos_s3",
                                    raw_message=f"El archivo '{filename}' ({int(content_length)/(1024*1024):.2f}MB) supera el límite máximo de 30MB.",
                                    crm_action="Verifique el tamaño del archivo en la SFC."
                                )

                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            tmp_file.write(chunk)
                            downloaded_bytes += len(chunk)
                            
                            if downloaded_bytes > max_bytes:
                                raise SfcIntegrationException(
                                    status_code=400,
                                    error_type="FILE_SIZE_EXCEEDED",
                                    sfc_field="archivos_s3",
                                    raw_message=f"El archivo '{filename}' superó el límite máximo de 30MB durante la descarga en streaming.",
                                    crm_action="Verifique el tamaño del archivo en la SFC."
                                )

                    tmp_file.seek(0)
                    self.validar_integridad_archivo(file_data=tmp_file, file_name=filename)
                    
                    await self.subir_stream_archivo(
                        s3_key=s3_key,
                        file_obj=tmp_file,
                        content_type=content_type
                    )

                    return {
                        "nombre_archivo": filename,
                        "s3_key": s3_key,
                        "bucket": self.default_bucket
                    }
                except SfcIntegrationException:
                    raise
                except Exception as e:
                    logger.error(f"❌ Error transfiriendo adjunto '{filename}' a S3: {e}")
                    if self.is_local:
                        return {
                            "nombre_archivo": filename,
                            "s3_key": s3_key,
                            "bucket": self.default_bucket
                        }
                    return None
                finally:
                    tmp_file.close()

        if self.http_client:
            tasks = [_procesar_adjunto(self.http_client, adj) for adj in adjuntos_sfc]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                tasks = [_procesar_adjunto(client, adj) for adj in adjuntos_sfc]
                results = await asyncio.gather(*tasks, return_exceptions=True)

        adjuntos_validos = []
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"❌ [S3 Storage] Excepción durante transferencia M1 desde SFC a S3: {res}")
            elif res is not None:
                adjuntos_validos.append(res)

        return adjuntos_validos

    async def transferir_lote_s3_a_sfc(
        self,
        sfc_client,
        sfc_codigo_queja: str,
        adjuntos_crm: List[Any],
        target_file_name: Optional[str] = None,
        afijo_regulatorio: Optional[str] = None,
        afijo_masivo: bool = False,
        case_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if not adjuntos_crm:
            return []

        # 🟢 FIX P0-10: checkpoint durable por archivo. Antes de reintentar el lote, se
        # consulta qué archivos de ESTE caso ya fueron confirmados por la SFC en un
        # intento previo, para no reenviarlos — en vez de depender únicamente de que la
        # SFC detecte el duplicado por su cuenta.
        checkpoint_service = IdempotencyService(get_redis_client())
        archivos_ya_completados = await checkpoint_service.obtener_archivos_completados(sfc_codigo_queja)

        sem = asyncio.Semaphore(5)

        async def _procesar_envio(item: Any) -> Optional[Dict[str, Any]]:
            async with sem:
                s3_key = item.s3_key if hasattr(item, "s3_key") else item.get("s3_key")
                bucket = (item.bucket if hasattr(item, "bucket") else item.get("bucket")) or self.default_bucket
                original_name = (item.nombre_archivo if hasattr(item, "nombre_archivo") else item.get("nombre_archivo")) or (item.get("nombre") if isinstance(item, dict) else None)

                if not original_name and s3_key:
                    original_name = s3_key.split("/")[-1]
                if not original_name:
                    return None

                identificador_archivo = s3_key or original_name
                if identificador_archivo in archivos_ya_completados:
                    logger.info(
                        f"⏭️ [S3 Storage] Archivo '{original_name}' ya estaba confirmado por checkpoint "
                        f"previo para {sfc_codigo_queja}; se omite el reenvío."
                    )
                    return {"file_name": original_name, "status": "ALREADY_CONFIRMED_CHECKPOINT"}

                file_type = original_name.split(".")[-1] if "." in original_name else "pdf"

                raw_bytes_input = item.get("bytes") if isinstance(item, dict) and item.get("bytes") else None
                file_obj_or_bytes = None
                tmp_stream = None
                
                try:
                    if not raw_bytes_input:
                        tmp_stream = await self.obtener_stream_archivo(
                            s3_key=s3_key, bucket=bucket, case_id_esperado=case_id
                        )
                        file_obj_or_bytes = tmp_stream
                    else:
                        file_obj_or_bytes = io.BytesIO(raw_bytes_input)
                        self.validar_integridad_archivo(file_data=file_obj_or_bytes, file_name=original_name)
                        
                    if hasattr(file_obj_or_bytes, "seek") and callable(file_obj_or_bytes.seek):
                        file_obj_or_bytes.seek(0)
                        
                    debe_aplicar_afijo = afijo_masivo or (target_file_name and original_name == target_file_name)
                    
                    if debe_aplicar_afijo and afijo_regulatorio and afijo_regulatorio not in original_name:
                        nombre_puro = original_name.rsplit(".", 1)[0]
                        final_send_name = f"{nombre_puro}_{afijo_regulatorio}.{file_type}"
                        logger.info(f"[S3 Orquestador] Inyectando afijo '{afijo_regulatorio}': '{original_name}' -> '{final_send_name}'")
                    else:
                        final_send_name = original_name

                    res_sfc = await sfc_client.post_adjunto_queja(
                        sfc_codigo_queja=sfc_codigo_queja,
                        file_data=file_obj_or_bytes,
                        file_type=file_type,
                        file_name=final_send_name
                    )
                    logger.info(f"✅ Adjunto '{final_send_name}' transmitido exitosamente a la SFC.")

                    # 🟢 FIX P0-10: checkpoint INMEDIATO tras el éxito de ESTE archivo — no se
                    # espera a que termine el lote completo, para no perder el progreso ya
                    # confirmado si otro archivo del mismo lote falla después.
                    await checkpoint_service.marcar_archivo_completado(
                        sfc_codigo_queja, identificador_archivo, metadata={"file_name": final_send_name}
                    )

                    return {"file_name": final_send_name, "status": "OK", "sfc_response": res_sfc}

                except SfcIntegrationException as exc:
                    raw_msg = (getattr(exc, "raw_message", "") or str(exc)).lower()
                    
                    es_duplicado_o_cerrado = (
                        getattr(exc, "error_type", None) == "DUPLICATE_FILE" 
                        or "ya existe" in raw_msg 
                        or "556240" in raw_msg
                        or "ya cuenta con un documento" in raw_msg
                        or "se encuentra cerrada" in raw_msg
                    )

                    if es_duplicado_o_cerrado:
                        logger.warning(
                            f"⚠️ [S3 Storage] Archivo '{original_name}' omitido en SFC para {sfc_codigo_queja}: "
                            f"Ya se encontraba registrado o el caso ya fue cerrado."
                        )
                        # 🟢 FIX P0-10: también se registra el checkpoint aquí — la SFC ya
                        # considera este archivo resuelto (duplicado o caso cerrado), así que
                        # un reintento futuro tampoco debe volver a intentarlo.
                        await checkpoint_service.marcar_archivo_completado(
                            sfc_codigo_queja, identificador_archivo, metadata={"file_name": original_name, "status": "DUPLICATE_OMITTED"}
                        )
                        return {"file_name": original_name, "status": "DUPLICATE_OMITTED"}
                    else:
                        raise
                except Exception as e:
                    logger.error(f"❌ Error al transferir '{original_name}' a la SFC: {e}")
                    raise
                finally:
                    if tmp_stream:
                        tmp_stream.close()

        tasks = [_procesar_envio(item) for item in adjuntos_crm]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        envios_exitosos = []
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"❌ [S3 Storage] Excepción durante transferencia de adjunto a la SFC: {res}")
                raise res
            elif res is not None:
                envios_exitosos.append(res)

        return envios_exitosos