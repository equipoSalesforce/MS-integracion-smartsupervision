# app/services/momento_3_sync.py
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
from datetime import datetime
from zoneinfo import ZoneInfo

from app.integrations.sfc_client import SfcClient
from app.services.s3_service import S3StorageService
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
        self.s3_service = S3StorageService(s3_client=s3_client)

    async def ejecutar_actualizacion_tramite(self, payload: Any) -> Dict[str, Any]:
        return await self._orquestar_pipeline_momento_3(payload=payload)

    async def ejecutar_gestion_fraude(self, payload: Any) -> Dict[str, Any]:
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            afijo_regulatorio="INV_FRAUDE_SFC",
            afijo_masivo=True  # 👈 Aplicar afijo a TODOS los archivos del directorio
        )
        
    

    async def ejecutar_cierre_definitivo(self, payload: Any) -> Dict[str, Any]:
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
        generar_pdf_cierre: bool = False,
        afijo_masivo: bool = False
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

        # 🎯 AISLAMIENTO DE INTENCIONES:
        if generar_pdf_cierre:
            # Flujo de Cierre: Forzar estado 4 (Cerrado), documentacion_rta_final en True y asegurar fecha_cierre
            sfc_raw_payload["estado_cod"] = 4
            sfc_raw_payload["documentacion_rta_final"] = True
            if not sfc_raw_payload.get("fecha_cierre"):
                sfc_raw_payload["fecha_cierre"] = datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT%H:%M:%S")
        else:
            # Flujo Intermedio (Trámite o Fraude): Remover rastros de cierre para no violar reglas de la SFC
            sfc_raw_payload["estado_cod"] = 2
            sfc_raw_payload["fecha_cierre"] = None
            sfc_raw_payload["documentacion_rta_final"] = False
            sfc_raw_payload["a_favor_de"] = None
            sfc_raw_payload["aceptacion_queja"] = None

        estado_cod = sfc_raw_payload.get("estado_cod", 2)

        try:
            pdf_generado_exito = False
            if generar_pdf_cierre and cuerpo_correo:
                await self._generar_y_enviar_pdf_respuesta_final(
                    sfc_code=sfc_id_largo,
                    cuerpo_correo_html=cuerpo_correo,
                    cliente_nombre=cliente_nombre
                )
                pdf_generado_exito = True

            if archivos_s3_raw:
                await self._procesar_y_enviar_adjuntos_m3(
                    archivos=archivos_s3_raw,
                    sfc_code=sfc_id_largo,
                    target_file_name=target_file_name,
                    afijo_regulatorio=afijo_regulatorio,
                    afijo_masivo=afijo_masivo
                )

            sfc_raw_payload["codigo_queja"] = sfc_id_largo
            sfc_raw_payload["anexo_queja"] = pdf_generado_exito or len(archivos_s3_raw) > 0
            sfc_raw_payload["fecha_actualizacion"] = datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT%H:%M:%S")

            if pdf_generado_exito:
                sfc_raw_payload["documentacion_rta_final"] = True
            
            if not target_file_name and not afijo_regulatorio:
                sfc_raw_payload["tipo_fraude"] = None
                sfc_raw_payload["modalidad_fraude"] = None
                sfc_raw_payload["monto_reclamado"] = None
                sfc_raw_payload["monto_reconocido"] = None
            
            sfc_defaults = {
                "condicion_especial": 98, "queja_expres": 2, 
                "tutela": 2, "ente_control": 99, "producto_digital": 1, "admision": 1, 
                "desistimiento_queja": 2
            }
            for campo, valor_defecto in sfc_defaults.items():
                if campo not in sfc_raw_payload or sfc_raw_payload[campo] is None:
                    sfc_raw_payload[campo] = valor_defecto

            payload_validado = SfcActualizarQuejaPayload(**sfc_raw_payload)

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

    async def _generar_y_enviar_pdf_respuesta_final(
        self,
        sfc_code: str,
        cuerpo_correo_html: str,
        cliente_nombre: str
    ):
        """
        Genera el PDF con la respuesta final limpia y lo transmite a S3 y a la SFC.
        Ejecuta tanto el parsing HTML como la renderización en un hilo worker aislado.
        """
        # 🎯 Trabajo CPU-Bound encapsulado para ejecutarse fuera del Event Loop
        def _job_parsing_y_renderizado(ruta_pdf: Path):
            texto_limpio = extraer_texto_limpio_de_html(cuerpo_correo_html)
            if not texto_limpio.strip():
                texto_limpio = (
                    "Se emite respuesta formal y cierre definitivo al caso de reclamación "
                    "conforme a los términos de ley y políticas de la entidad."
                )
            
            generar_pdf_respuesta_final(
                caso_nombre=cliente_nombre,
                smart_code=sfc_code,
                texto_crm=texto_limpio,
                ruta_salida=ruta_pdf
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / f"Respuesta_Final_{sfc_code}.pdf"

            # 🚀 Parsing HTML + Generación PDF en hilo de fondo
            await asyncio.to_thread(_job_parsing_y_renderizado, pdf_path)

            with open(pdf_path, "rb") as f:
                file_bytes = f.read()

            final_pdf_name = f"Respuesta_Final_{sfc_code}_RESP_FINAL_SFC.pdf"
            
            # Guardar respaldo en S3
            s3_key = f"caso/cierre/{sfc_code}/{final_pdf_name}"
            try:
                await self.s3_service.subir_bytes_archivo(
                    s3_key=s3_key,
                    file_bytes=file_bytes,
                    content_type="application/pdf"
                )
            except Exception as s3_err:
                logger.error(f"⚠️ [Momento 3] No se pudo guardar el respaldo del PDF en S3: {s3_err}")

            # Enviar adjunto a la SFC
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
        afijo_regulatorio: Optional[str],
        afijo_masivo: bool = False
    ):
        async def _procesar_un_adjunto(item):
            s3_key = item.s3_key if hasattr(item, "s3_key") else item.get("s3_key")
            bucket = item.bucket if hasattr(item, "bucket") else item.get("bucket")
            original_name = item.nombre_archivo if hasattr(item, "nombre_archivo") else item.get("nombre_archivo")
            file_type = original_name.split(".")[-1] if "." in original_name else "pdf"

            file_bytes = await self.s3_service.obtener_bytes_archivo(s3_key=s3_key, bucket=bucket)

            # 🎯 Lógica de Afijo: Masivo para todos en fraude o puntual para objetivo
            debe_aplicar_afijo = afijo_masivo or (target_file_name and original_name == target_file_name)

            if debe_aplicar_afijo and afijo_regulatorio and afijo_regulatorio not in original_name:
                nombre_puro = original_name.rsplit(".", 1)[0]
                final_send_name = f"{nombre_puro}_{afijo_regulatorio}.{file_type}"
                logger.info(f"[Momento 3] Aplicando afijo '{afijo_regulatorio}' a '{original_name}' -> '{final_send_name}'")
            else:
                final_send_name = original_name

            return await self._enviar_adjunto_seguro(
                sfc_code=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type,
                file_name=final_send_name
            )

        tareas = [_procesar_un_adjunto(item) for item in archivos]
        await asyncio.gather(*tareas)

    async def _enviar_adjunto_seguro(
        self, sfc_code: str, file_bytes: bytes, file_type: str, file_name: str
    ):
        try:
            await self.sfc_client.post_adjunto_queja(
                sfc_codigo_queja=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type,
                file_name=file_name
            )
        except SfcIntegrationException as exc:
            if getattr(exc, "error_type", None) == "DUPLICATE_FILE":
                logger.warning(f"[Momento 3] Archivo '{file_name}' duplicado en SFC. Omitiendo.")
                return
            raw_msg = (getattr(exc, "raw_message", "") or str(exc)).lower()
            if "ya existe" in raw_msg or "556240" in raw_msg:
                logger.warning(f"[Momento 3] Fallback texto duplicado en '{file_name}'. Omitiendo.")
                return
            raise