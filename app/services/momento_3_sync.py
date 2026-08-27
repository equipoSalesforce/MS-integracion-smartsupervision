# app/services/momento_3_sync.py
import asyncio
import logging
import time
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from app.integrations.sfc_client import SfcClient
from app.services.s3_service import S3StorageService
from app.core.exceptions import SfcIntegrationException, resumir_validation_error_sin_pii
from app.core.mapping import SfcSalesforceMapper
from app.schemas.sfc_payloads import SfcActualizarQuejaPayload
from app.utils.email_parser import extraer_texto_limpio_de_html
from app.utils.pdf_generator import generar_pdf_respuesta_final
from app.services.email_service import EmailAlertService
from app.core.metrics import emit_emf_metric
from app.core.config import settings

logger = logging.getLogger(__name__)


class Momento3SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_service = S3StorageService(s3_client=s3_client, http_client=getattr(sfc_client, "client", None))

    async def ejecutar_actualizacion_tramite(self, payload: Any) -> Dict[str, Any]:
        return await self._orquestar_pipeline_momento_3(payload=payload)

    async def ejecutar_gestion_fraude(self, payload: Any) -> Dict[str, Any]:
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            afijo_regulatorio="INV_FRAUDE_SFC",
            afijo_masivo=True
        )

    async def ejecutar_cierre_definitivo(self, payload: Any) -> Dict[str, Any]:
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            generar_pdf_cierre=True,
            afijo_regulatorio="RESP_FINAL_SFC"
        )

    @staticmethod
    def _extraer_datos_payload(payload: Any) -> Tuple[Dict[str, Any], str, str, list, Optional[str], str]:
        """🟢 EXTRACCIÓN SEGURA DE PROPIEDADES (Pydantic Model vs Dict)."""
        if isinstance(payload, dict):
            crm_dict = payload
            smart_code = payload.get("Smart_Code__c") or payload.get("Case_id")
            case_id_crm = payload.get("Case_id") or smart_code
            archivos_s3_raw = payload.get("archivos_s3", [])
            cuerpo_correo = payload.get("cuerpo_respuesta_final")
            cliente_nombre = payload.get("SuppliedName", "Consumidor Financiero")
        else:
            crm_dict = payload.model_dump()
            smart_code = payload.Smart_Code__c
            case_id_crm = payload.Case_id or smart_code
            archivos_s3_raw = payload.archivos_s3
            cuerpo_correo = getattr(payload, "cuerpo_respuesta_final", None)
            cliente_nombre = getattr(payload, "SuppliedName", "Consumidor Financiero")

        return crm_dict, smart_code, case_id_crm, archivos_s3_raw, cuerpo_correo, cliente_nombre

    @staticmethod
    def _determinar_sub_operacion(generar_pdf_cierre: bool, afijo_regulatorio: Optional[str]) -> str:
        if generar_pdf_cierre:
            return "cierre"
        if afijo_regulatorio == "INV_FRAUDE_SFC":
            return "fraude"
        return "actualizacion"

    @staticmethod
    def _emitir_metrica_m3(
        sub_operacion: str, tiene_adjuntos: bool, inicio_monotonic: float, resultado: str, categoria_error: str = "N/A"
    ) -> None:
        """
        Métrica EMF de latencia y volumen del pipeline de Momento 3, por
        sub-operación -- pedida en la propuesta de observabilidad de CX
        (Grafana), panel "M3 por sub-operación". `tiene_adjuntos` emite además un
        conteo bajo sub_operacion='adjunto', independiente de cierre/fraude/
        actualizacion (un mismo caso puede ser, por ejemplo, un cierre que
        además trae adjuntos).
        """
        latencia_ms = (time.monotonic() - inicio_monotonic) * 1000
        dims_base = {"Environment": settings.ENVIRONMENT, "resultado": resultado, "categoria_error": categoria_error}

        emit_emf_metric(
            namespace="SSV/MomentoTres",
            metrics={"m3_count": (1, "Count"), "m3_latency_ms": (latencia_ms, "Milliseconds")},
            dimensions={**dims_base, "sub_operacion": sub_operacion}
        )
        if tiene_adjuntos:
            emit_emf_metric(
                namespace="SSV/MomentoTres",
                metrics={"m3_count": (1, "Count")},
                dimensions={**dims_base, "sub_operacion": "adjunto"}
            )

    @staticmethod
    def _aplicar_estado_inicial_sfc(sfc_raw_payload: Dict[str, Any], generar_pdf_cierre: bool) -> None:
        if generar_pdf_cierre:
            sfc_raw_payload["estado_cod"] = 4
            # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): antes se fijaba en True
            # acá mismo, ANTES de intentar generar el PDF de respuesta final -- si
            # cuerpo_correo llegaba vacío (_orquestar_pipeline_momento_3 sólo genera el
            # PDF `if generar_pdf_cierre and cuerpo_correo`), documentacion_rta_final
            # quedaba en True aunque el PDF nunca se generó ni se transmitió. Se deja en
            # False acá; la única fuente de verdad es `if pdf_generado_exito:
            # sfc_raw_payload["documentacion_rta_final"] = True` más abajo, después de
            # confirmar que el PDF realmente se generó.
            #
            # Nota: hoy esto no es alcanzable por la vía pública real -- el
            # model_validator _validar_reglas_cierre en QuejaUnificadaCrmInput ya
            # autorrellena cuerpo_respuesta_final con un texto por defecto si viene
            # vacío, y _orquestar_pipeline_momento_3 sólo se invoca con ese modelo ya
            # validado. Se corrige de todas formas: es una fuente de verdad duplicada
            # (y potencialmente divergente) para un campo que reporta a un ente
            # regulador financiero si un documento fue realmente entregado.
            sfc_raw_payload["documentacion_rta_final"] = False
            if not sfc_raw_payload.get("fecha_cierre"):
                sfc_raw_payload["fecha_cierre"] = datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT%H:%M:%S")
        else:
            sfc_raw_payload["estado_cod"] = 2
            sfc_raw_payload["fecha_cierre"] = None
            sfc_raw_payload["documentacion_rta_final"] = False
            sfc_raw_payload["a_favor_de"] = None
            sfc_raw_payload["aceptacion_queja"] = None

    @staticmethod
    def _aplicar_defaults_finales_sfc(
        sfc_raw_payload: Dict[str, Any], afijo_regulatorio: Optional[str], crm_dict: Dict[str, Any]
    ) -> None:
        es_pipeline_fraude = (afijo_regulatorio == "INV_FRAUDE_SFC") or (
            crm_dict.get("tipo_fraude__c") is not None or crm_dict.get("modalidad_fraude__c") is not None
        )

        if not es_pipeline_fraude:
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

    async def _orquestar_pipeline_momento_3(
        self,
        payload: Any,
        target_file_name: Optional[str] = None,
        afijo_regulatorio: Optional[str] = None,
        generar_pdf_cierre: bool = False,
        afijo_masivo: bool = False
    ) -> Dict[str, Any]:
        inicio_monotonic = time.monotonic()
        crm_dict, smart_code, case_id_crm, archivos_s3_raw, cuerpo_correo, cliente_nombre = self._extraer_datos_payload(payload)
        sub_operacion = self._determinar_sub_operacion(generar_pdf_cierre, afijo_regulatorio)

        sfc_id_largo = smart_code
        sfc_raw_payload = SfcSalesforceMapper.crm_entity_to_sfc_payload(crm_dict, momento=3)

        self._aplicar_estado_inicial_sfc(sfc_raw_payload, generar_pdf_cierre)
        estado_cod = sfc_raw_payload.get("estado_cod", 2)

        try:
            pdf_generado_exito = False
            if generar_pdf_cierre and cuerpo_correo:
                await self._generar_y_enviar_pdf_respuesta_final(
                    case_id=case_id_crm,
                    sfc_code=sfc_id_largo,
                    cuerpo_correo_html=cuerpo_correo,
                    cliente_nombre=cliente_nombre
                )
                pdf_generado_exito = True

            # DELEGACIÓN AL S3 STORAGE SERVICE PARA ADJUNTOS Y AFIJOS DE M3
            if archivos_s3_raw:
                await self.s3_service.transferir_lote_s3_a_sfc(
                    sfc_client=self.sfc_client,
                    sfc_codigo_queja=sfc_id_largo,
                    adjuntos_crm=archivos_s3_raw,
                    target_file_name=target_file_name,
                    afijo_regulatorio=afijo_regulatorio,
                    afijo_masivo=afijo_masivo,
                    case_id=case_id_crm  # 🟢 FIX P1-10: sólo para validar ownership del s3_key
                )

            sfc_raw_payload["codigo_queja"] = sfc_id_largo
            sfc_raw_payload["anexo_queja"] = pdf_generado_exito or len(archivos_s3_raw) > 0
            sfc_raw_payload["fecha_actualizacion"] = datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT%H:%M:%S")

            if pdf_generado_exito:
                sfc_raw_payload["documentacion_rta_final"] = True

            self._aplicar_defaults_finales_sfc(sfc_raw_payload, afijo_regulatorio, crm_dict)

            payload_validado = SfcActualizarQuejaPayload(**sfc_raw_payload)

            await self.sfc_client.put_actualizar_queja(
                sfc_codigo_queja=sfc_id_largo, 
                payload=payload_validado.model_dump(exclude_none=True)
            )

            self._emitir_metrica_m3(sub_operacion, bool(archivos_s3_raw), inicio_monotonic, resultado="success")
            return {
                "status": "success",
                "message": f"Caso {smart_code} actualizado en M3 (Estado SFC {estado_cod})",
                "codigo_queja_sfc": sfc_id_largo
            }

        except SfcIntegrationException as exc:
            if getattr(exc, "is_unmapped", False) or getattr(exc, "error_type", None) == "UNKNOWN_SFC_ERROR":
                await EmailAlertService.notificar_error_no_mapeado(
                    status_code=getattr(exc, "status_code", 500),
                    raw_message=str(exc),
                    sfc_field=getattr(exc, "sfc_field", None),
                    smart_code=smart_code
                )
            self._emitir_metrica_m3(
                sub_operacion, bool(archivos_s3_raw), inicio_monotonic,
                resultado="error", categoria_error=getattr(exc, "error_type", None) or "UNKNOWN_SFC_ERROR"
            )
            raise

        except (httpx.RequestError, httpx.TimeoutException, ConnectionError, OSError) as net_err:
            logger.error(f"❌ [Momento 3] Fallo de red/conexión para {smart_code}: {net_err}")
            self._emitir_metrica_m3(sub_operacion, bool(archivos_s3_raw), inicio_monotonic, resultado="error", categoria_error="NETWORK_ERROR")
            raise net_err

        except ValidationError as ve:
            # 🔴 FIX (hallazgo propio, 2026-08-27, defensa en profundidad -- mismo
            # patrón que momento_2_sync.py): SfcActualizarQuejaPayload no incluye PII
            # directa hoy, pero se captura ANTES del `except Exception` genérico de
            # abajo para que un campo con PII agregado a futuro a ese schema no
            # reintroduzca el mismo leak. Se relanza la MISMA excepción sin envolver.
            logger.error(f"🔥 [Momento 3] Payload SFC inválido para caso {smart_code}: {resumir_validation_error_sin_pii(ve)}")
            self._emitir_metrica_m3(sub_operacion, bool(archivos_s3_raw), inicio_monotonic, resultado="error", categoria_error="VALIDATION_ERROR")
            raise

        except Exception as e:
            # 🟢 FIX HALLAZGO 40: Se relanza la excepción no controlada para tratarse como 500
            logger.error(f"🔥 [Momento 3] Fallo no controlado en pipeline M3 para caso {smart_code}: {str(e)}", exc_info=True)
            self._emitir_metrica_m3(sub_operacion, bool(archivos_s3_raw), inicio_monotonic, resultado="error", categoria_error="UNCONTROLLED_ERROR")
            raise e

    async def _generar_y_enviar_pdf_respuesta_final(
        self,
        case_id: str,
        sfc_code: str,
        cuerpo_correo_html: str,
        cliente_nombre: str
    ):
        def _job_parsing_y_renderizado() -> bytes:
            texto_limpio = extraer_texto_limpio_de_html(cuerpo_correo_html)
            if not texto_limpio.strip():
                texto_limpio = "Se emite respuesta formal y cierre definitivo al caso de reclamación."
            return generar_pdf_respuesta_final(
                caso_nombre=cliente_nombre,
                smart_code=case_id,
                texto_crm=texto_limpio
            )

        file_bytes = await asyncio.to_thread(_job_parsing_y_renderizado)
        
        final_pdf_name = f"Respuesta_Final_{case_id}_RESP_FINAL_SFC.pdf"
        s3_key = f"caso/{case_id}/{final_pdf_name}"

        try:
            await self.s3_service.subir_bytes_archivo(
                s3_key=s3_key,
                file_bytes=file_bytes,
                content_type="application/pdf"
            )
        except Exception as s3_err:
            logger.error(f"⚠️ [Momento 3] No se pudo guardar la copia del PDF en S3: {s3_err}")

        await self.s3_service.transferir_lote_s3_a_sfc(
            sfc_client=self.sfc_client,
            sfc_codigo_queja=sfc_code,
            adjuntos_crm=[{"nombre_archivo": final_pdf_name, "s3_key": s3_key, "bytes": file_bytes}],
            case_id=case_id  # 🟢 FIX P1-10 (no-op aquí: bytes ya vienen inline, no se lee de S3)
        )