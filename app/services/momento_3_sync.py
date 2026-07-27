# app/services/momento_3_sync.py
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
from datetime import datetime
from zoneinfo import ZoneInfo
from botocore.exceptions import ClientError

from app.integrations.sfc_client import SfcClient
from app.core.config import settings
from app.core.exceptions import SfcIntegrationException
from app.core.mapping import SfcSalesforceMapper 
from app.schemas.sfc_payloads import SfcActualizarQuejaPayload 
from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.utils.email_parser import extraer_texto_limpio_de_html
from app.utils.pdf_generator import generar_pdf_respuesta_final
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)


class Momento3SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_client = s3_client
        self.tipo_entidad = settings.SFC_TIPO_ENTIDAD
        self.entidad_cod = settings.SFC_ENTIDAD_COD

    async def ejecutar_actualizacion_tramite(
        self, payload: Union[QuejaUnificadaCrmInput, Dict[str, Any], Any]
    ) -> Dict[str, Any]:
        return await self._orquestar_pipeline_momento_3(payload=payload)

    async def ejecutar_gestion_fraude(
        self, payload: Union[QuejaUnificadaCrmInput, Dict[str, Any], Any]
    ) -> Dict[str, Any]:
        target_file_name = (
            getattr(payload, "nombre_archivo_fraude", None) 
            if not isinstance(payload, dict) 
            else payload.get("nombre_archivo_fraude")
        )
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            target_file_name=target_file_name,
            afijo_regulatorio="INV_FRAUDE_SFC"
        )

    async def ejecutar_cierre_definitivo(
        self, payload: Union[QuejaUnificadaCrmInput, Dict[str, Any], Any]
    ) -> Dict[str, Any]:
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            generar_pdf_cierre=True,
            afijo_regulatorio="RESP_FINAL_SFC"
        )

    async def _orquestar_pipeline_momento_3(
        self, 
        payload: Any, 
        target_file_name: Optional[str] = None, 
        afijo_regulatorio: Optional[str] = None,
        generar_pdf_cierre: bool = False
    ) -> Dict[str, Any]:
        if isinstance(payload, dict):
            crm_dict = payload
            smart_code = payload.get("Smart_Code__c") or payload.get("Case_id")
            archivos_s3_raw = payload.get("archivos_s3", [])
            cuerpo_correo = payload.get("cuerpo_respuesta_final")
            cliente_nombre = payload.get("SuppliedName", "Consumidor Financiero")
        else:
            crm_dict = payload.model_dump()
            smart_code = payload.Smart_Code__c
            archivos_s3_raw = payload.archivos_s3
            cuerpo_correo = getattr(payload, "cuerpo_respuesta_final", None)
            cliente_nombre = getattr(payload, "SuppliedName", "Consumidor Financiero")

        sfc_id_largo = smart_code
        sfc_raw_payload = SfcSalesforceMapper.crm_entity_to_sfc_payload(crm_dict, momento=3)
        estado_cod = sfc_raw_payload.get("estado_cod", 2)

        logger.info(f"[Momento 3] Iniciando pipeline asíncrono para el caso: {sfc_id_largo} (Estado SFC: {estado_cod})")

        try:
            pdf_generado_exito = False
            if generar_pdf_cierre and cuerpo_correo:
                logger.info(f"[Momento 3] Generando PDF de respuesta final a partir de 'cuerpo_respuesta_final'...")
                await self._generar_y_enviar_pdf_respuesta_final(
                    sfc_code=sfc_id_largo,
                    cuerpo_correo_html=cuerpo_correo,
                    cliente_nombre=cliente_nombre
                )
                pdf_generado_exito = True

            if archivos_s3_raw:
                logger.info(f"[Momento 3] Detectados {len(archivos_s3_raw)} anexos en S3. Iniciando carga previa...")
                await self._procesar_y_enviar_adjuntos_m3(
                    archivos=archivos_s3_raw,
                    sfc_code=sfc_id_largo,
                    target_file_name=target_file_name,
                    afijo_regulatorio=afijo_regulatorio
                )

            sfc_raw_payload["codigo_queja"] = sfc_id_largo
            sfc_raw_payload["anexo_queja"] = pdf_generado_exito or len(archivos_s3_raw) > 0
            sfc_raw_payload["fecha_actualizacion"] = datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT%H:%M:%S")

            if pdf_generado_exito:
                sfc_raw_payload["documentacion_rta_final"] = True
            
            sfc_defaults = {
                "sexo": 2, "lgbtiq": 2, "condicion_especial": 98,
                "queja_expres": 1, "tutela": 2, "ente_control": 99,
                "producto_digital": 1, "admision": 1, "desistimiento_queja": 2,
                "tipo_fraude": 0, "modalidad_fraude": 0, "monto_reclamado": 0.0, "monto_reconocido": 0.0,
                "prorroga_queja": 1
            }
            for campo, valor_defecto in sfc_defaults.items():
                if campo not in sfc_raw_payload or sfc_raw_payload[campo] is None:
                    sfc_raw_payload[campo] = valor_defecto

            payload_validado = SfcActualizarQuejaPayload(**sfc_raw_payload)

            logger.info(f"[Momento 3] Transmitiendo formulario de actualización de estado hacia la SFC...")
            await self.sfc_client.put_actualizar_queja(
                sfc_codigo_queja=sfc_id_largo, 
                payload=payload_validado.model_dump(exclude_none=True)
            )

            return {
                "status": "success",
                "message": f"Caso {smart_code} actualizado exitosamente en el Momento 3 (Estado SFC {estado_cod})",
                "codigo_queja_sfc": sfc_id_largo
            }

        except SfcIntegrationException as exc:
            if getattr(exc, "is_unmapped", False) or getattr(exc, "error_type", None) == "UNKNOWN_ERROR":
                await EmailAlertService.notificar_error_no_mapeado(
                    status_code=getattr(exc, "status_code", 500),
                    raw_message=str(exc),
                    sfc_field=getattr(exc, "sfc_field", None),
                    smart_code=smart_code
                )
            raise

        except Exception as e:
            logger.error(f"Fallo crítico en pipeline del Momento 3 para caso {smart_code}: {str(e)}")
            return {"status": "error", "message": f"Pipeline M3 interrumpido: {str(e)}"}

    async def _enviar_adjunto_seguro(
        self, 
        sfc_code: str, 
        file_bytes: bytes, 
        file_type: str, 
        file_name: str
    ):
        """
        Envía un adjunto a la SFC manejando de forma idempotente el error 'DUPLICATE_FILE'.
        Si el archivo ya fue cargado previamente en la SFC, omite la falla y permite continuar el flujo.
        """
        try:
            await self.sfc_client.post_adjunto_queja(
                sfc_codigo_queja=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type,
                file_name=file_name
            )
        except SfcIntegrationException as exc:
            # 🎯 1. Búsqueda directa basada en la clasificación del catálogo JSON (DUPLICATE_FILE)
            if getattr(exc, "error_type", None) == "DUPLICATE_FILE":
                logger.warning(
                    f"[Momento 3] El archivo '{file_name}' ya fue identificado como DUPLICATE_FILE en la SFC. "
                    f"Se omite la falla y se continúa con el proceso del caso {sfc_code}."
                )
                return

            # 🎯 2. Fallback secundario defensivo sobre raw_message o mensaje completo
            raw_msg = (getattr(exc, "raw_message", "") or str(exc)).lower()
            if "ya existe" in raw_msg or "556240" in raw_msg:
                logger.warning(
                    f"[Momento 3] Fallback texto: El archivo '{file_name}' ya existía en la SFC. "
                    f"Ignorando duplicado para continuar el despacho del caso {sfc_code}."
                )
                return

            raise

        except Exception as e:
            error_msg = str(e).lower()
            if "ya existe" in error_msg or "556240" in error_msg:
                logger.warning(
                    f"[Momento 3] Excepción no controlada con patrón duplicado en '{file_name}'. Continuando..."
                )
                return
            raise

    async def _generar_y_enviar_pdf_respuesta_final(
        self,
        sfc_code: str,
        cuerpo_correo_html: str,
        cliente_nombre: str
    ):
        texto_limpio = extraer_texto_limpio_de_html(cuerpo_correo_html)

        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / f"Respuesta_Final_{sfc_code}.pdf"

            await asyncio.to_thread(
                generar_pdf_respuesta_final,
                caso_nombre=cliente_nombre,
                smart_code=sfc_code,
                texto_crm=texto_limpio,
                ruta_salida=pdf_path
            )

            with open(pdf_path, "rb") as f:
                file_bytes = f.read()

            final_pdf_name = f"Respuesta_Final_{sfc_code}_RESP_FINAL_SFC.pdf"
            
            if self.s3_client:
                s3_key = f"quejas/{sfc_code}/cierre/{final_pdf_name}"
                try:
                    logger.info(f"[Momento 3] Guardando copia de respaldo del PDF en S3: {s3_key}")
                    await asyncio.to_thread(
                        self.s3_client.put_object,
                        Bucket=settings.AWS_S3_BUCKET,
                        Key=s3_key,
                        Body=file_bytes,
                        ContentType="application/pdf"
                    )
                except Exception as s3_err:
                    logger.error(f"⚠️ [Momento 3] No se pudo guardar el respaldo del PDF en S3: {s3_err}")

            logger.info(f"[Momento 3] Transmitiendo PDF generado '{final_pdf_name}' a la SFC...")
            await self._enviar_adjunto_seguro(
                sfc_code=sfc_code,
                file_bytes=file_bytes,
                file_type="pdf",
                file_name=final_pdf_name
            )

    async def _procesar_y_enviar_adjuntos_m3(
        self, 
        archivos: List[Any], 
        sfc_code: str, 
        target_file_name: Optional[str], 
        afijo_regulatorio: Optional[str]
    ):
        tareas_envio = []

        if not self.s3_client:
            if settings.ENVIRONMENT == "development" or settings.ENVIRONMENT == "local":
                logger.info("[LOCAL TEST M3] Generando streams locales con inyección de afijos regulatorios.")
                for item in archivos:
                    nombre_archivo = item.nombre_archivo if hasattr(item, "nombre_archivo") else item.get("nombre_archivo")
                    file_type = nombre_archivo.split(".")[-1] if "." in nombre_archivo else "pdf"
                    
                    file_name = nombre_archivo
                    if target_file_name and nombre_archivo == target_file_name:
                        if afijo_regulatorio and afijo_regulatorio not in nombre_archivo:
                            nombre_puro = nombre_archivo.rsplit(".", 1)[0]
                            file_name = f"{nombre_puro}_{afijo_regulatorio}.{file_type}"
                        logger.info(f"[LOCAL TEST M3] Aplicando afijo. Nombre de envío: {file_name}")

                    file_bytes = b"Contenido de resolucion digital simulado por Global66."
                    tareas_envio.append(self._enviar_adjunto_seguro(
                        sfc_code=sfc_code,
                        file_bytes=file_bytes,
                        file_type=file_type,
                        file_name=file_name
                    ))
                if tareas_envio:
                    await asyncio.gather(*tareas_envio)
                return
            else:
                raise SfcIntegrationException(
                    status_code=500,
                    error_type="INFRASTRUCTURE_ERROR",
                    sfc_field="s3_client",
                    raw_message="El cliente de almacenamiento S3 no está inicializado en el entorno actual.",
                    crm_action="Contactar al equipo de infraestructura/DevOps para validar la configuración de AWS S3."
                )

        for item in archivos:
            s3_key = item.s3_key if hasattr(item, "s3_key") else item.get("s3_key")
            bucket = (item.bucket if hasattr(item, "bucket") else item.get("bucket")) or settings.AWS_S3_BUCKET
            original_name = item.nombre_archivo if hasattr(item, "nombre_archivo") else item.get("nombre_archivo")
            file_type = original_name.split(".")[-1] if "." in original_name else "pdf"

            # 🎯 1. Captura estandarizada de existencia en S3 / MinIO
            try:
                metadata = await asyncio.to_thread(self.s3_client.head_object, Bucket=bucket, Key=s3_key)
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code in ("404", "403", "NoSuchKey", "NotFound"):
                    logger.error(f"❌ [Momento 3 S3 Error] Archivo '{original_name}' no encontrado (Key: '{s3_key}', Bucket: '{bucket}').")
                    raise SfcIntegrationException(
                        status_code=404,
                        error_type="S3_FILE_NOT_FOUND",
                        sfc_field="archivos_s3",
                        raw_message=f"El archivo '{original_name}' (Key: '{s3_key}') no existe o no se pudo acceder en el almacenamiento S3.",
                        crm_action="Verifique que el archivo haya sido cargado correctamente en el bucket de S3/MinIO antes de reintentar la transmisión."
                    )
                raise

            # 🎯 2. Captura estandarizada de límite de tamaño (30MB)
            if metadata.get("ContentLength", 0) > 30 * 1024 * 1024:
                raise SfcIntegrationException(
                    status_code=400,
                    error_type="FILE_SIZE_EXCEEDED",
                    sfc_field="archivos_s3",
                    raw_message=f"El archivo '{original_name}' supera el límite máximo de 30MB permitido por la SFC.",
                    crm_action="Comprima el documento o adjunte una versión de menor tamaño (máximo 30MB)."
                )

            s3_file = await asyncio.to_thread(self.s3_client.get_object, Bucket=bucket, Key=s3_key)
            file_bytes = s3_file["Body"].read()

            if target_file_name and original_name == target_file_name:
                if afijo_regulatorio and afijo_regulatorio not in original_name:
                    nombre_puro = original_name.rsplit(".", 1)[0]
                    final_send_name = f"{nombre_puro}_{afijo_regulatorio}.{file_type}"
                else:
                    final_send_name = original_name
                logger.info(f"[Momento 3] Aplicando afijo oficial. Transmutando '{original_name}' a '{final_send_name}'")
            else:
                final_send_name = original_name

            tareas_envio.append(self._enviar_adjunto_seguro(
                sfc_code=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type,
                file_name=final_send_name
            ))

        if tareas_envio:
            logger.info(f"[Momento 3] Transmitiendo concurrentemente {len(tareas_envio)} anexos a la SFC.")
            await asyncio.gather(*tareas_envio)