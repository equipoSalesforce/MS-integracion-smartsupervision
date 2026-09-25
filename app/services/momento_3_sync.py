from app.services.crm_storage_contract import normalize_crm_storage
from app.core.dispatch_observability import capture_prepared
from app.core.reopen_diagnostic import capture_reopen_payload
# app/services/momento_3_sync.py
import asyncio
import hashlib
import json
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
from app.utils.pdf_generator import generar_pdf_respuesta_final, vincular_pdf_a_ciclo
from app.services.email_service import EmailAlertService
from app.core.metrics import emit_emf_metric
from app.core.config import settings
from app.db.redis import get_redis_client
from app.services.idempotency_service import IdempotencyService
from app.services.final_response_contract import (
    closure_identity, final_response_filename, is_final_response, order_close_attachments,
)

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

    @staticmethod
    def _respuesta_final_no_enviada(cause: Optional[Exception] = None) -> SfcIntegrationException:
        status_code = getattr(cause, "status_code", 503) if cause is not None else 409
        # A missing PDF is not a missing complaint: never trigger M2 self-healing.
        if status_code == 404:
            status_code = 409
        return SfcIntegrationException(
            status_code=status_code,
            error_type="FINAL_RESPONSE_DOCUMENT_NOT_SENT",
            sfc_field="documentacion_rta_final",
            raw_message="No fue posible enviar la respuesta final de la réplica antes del cierre.",
            crm_action="No fue posible enviar la respuesta final de la réplica antes del cierre.",
        )

    @staticmethod
    def _replica_stage(stage: str, close_id: str, *, file_name: str = "", status: str = "") -> None:
        safe = {"correlation_id": close_id, "nombre_archivo": file_name, "status": status}
        capture_prepared(safe, stage)
        logger.info("%s cycle_id=%s nombre_archivo=%s status=%s", stage, close_id, file_name, status)

    @staticmethod
    def _final_receipt_identity(case_id: str, sfc_code: str, close_id: str, *, replica: bool) -> dict:
        name = final_response_filename(case_id, close_id, replica=replica)
        return {"case_id": case_id, "smart_code": sfc_code, "cycle_id": close_id,
                "file_name": name, "s3_key": f"caso/{close_id}/{case_id}/{name}"}

    @staticmethod
    def _confirmed_final_receipt(receipt: Optional[dict], expected: dict, *, replica: bool) -> bool:
        if not receipt or receipt.get('sent') is not True:
            return False
        if not replica:
            return True  # Preserve the existing CLOSE receipt/idempotency contract.
        digest = receipt.get('sha256', '')
        return (all(receipt.get(k) == v for k, v in expected.items())
                and receipt.get('version') == 2 and receipt.get('upload_status') == 'OK'
                and isinstance(digest, str) and len(digest) == 64
                and all(char in '0123456789abcdef' for char in digest))

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
        storage_kwargs = {}
        if crm_dict.get("crm_case_uuid") is not None:
            storage_kwargs = {"crm_case_uuid": crm_dict["crm_case_uuid"]}
            directory, archivos_s3_raw = normalize_crm_storage(
                crm_dict["crm_case_uuid"], crm_dict.get("directorio_s3"), archivos_s3_raw
            )
            crm_dict = {**crm_dict, "directorio_s3": directory, "archivos_s3": archivos_s3_raw}

        sub_operacion = self._determinar_sub_operacion(generar_pdf_cierre, afijo_regulatorio)
        es_reclose = generar_pdf_cierre and crm_dict.get("crm_operation") == "RECLOSE"
        if es_reclose and not crm_dict.get("crm_reopen_operation_id"):
            raise self._respuesta_final_no_enviada()

        sfc_id_largo = smart_code
        sfc_raw_payload = SfcSalesforceMapper.crm_entity_to_sfc_payload(crm_dict, momento=3)

        self._aplicar_estado_inicial_sfc(sfc_raw_payload, generar_pdf_cierre)
        estado_cod = sfc_raw_payload.get("estado_cod", 2)
        capture_prepared(sfc_raw_payload, "M3_MAPPED_BEFORE_ATTACHMENTS")

        try:
            pdf_generado_exito = False
            close_checkpoint = None
            close_receipt = None
            close_id = None
            existing_final = False
            if generar_pdf_cierre:
                if es_reclose:
                    # Historical RESP_FINAL_SFC documents remain in CRM/S3 but are not
                    # retransmitted and never satisfy a later replica closing cycle.
                    archivos_s3_raw = [item for item in archivos_s3_raw if not is_final_response(item)]
                archivos_s3_raw = order_close_attachments(archivos_s3_raw)
                existing_final = not es_reclose and any(is_final_response(item) for item in archivos_s3_raw)
                close_checkpoint = IdempotencyService(get_redis_client())
                close_id = closure_identity(crm_dict)
                expected_receipt = self._final_receipt_identity(case_id_crm, sfc_id_largo, close_id, replica=es_reclose)
                try:
                    close_receipt = await close_checkpoint.obtener_respuesta_final(close_id)
                    if es_reclose and close_receipt and any(
                        k in close_receipt and close_receipt[k] != v for k, v in expected_receipt.items()
                    ):
                        raise self._respuesta_final_no_enviada()
                except Exception as receipt_error:
                    if es_reclose:
                        raise self._respuesta_final_no_enviada(receipt_error) from receipt_error
                    raise
                if es_reclose:
                    self._replica_stage("RECLOSE_CYCLE_RESOLVED", close_id)
                    self._replica_stage("REPLICA_RESP_FINAL_RESOLVED", close_id,
                                        file_name=expected_receipt['file_name'])
                if self._confirmed_final_receipt(close_receipt, expected_receipt, replica=es_reclose):
                    # An accepted replica is already LAST. A same-cycle retry only
                    # resumes PATCH; it cannot put ordinary attachments after it.
                    archivos_s3_raw = [] if es_reclose else [item for item in archivos_s3_raw if not is_final_response(item)]
                    pdf_generado_exito = True
                    if es_reclose:
                        self._replica_stage("REPLICA_RESP_FINAL_REUSED_CURRENT_CYCLE", close_id,
                                            file_name=expected_receipt['file_name'], status="ACCEPTED")
                        self._replica_stage("REPLICA_RESP_FINAL_CHECKPOINT_CONFIRMED", close_id, status="ACCEPTED")

            # DELEGACIÓN AL S3 STORAGE SERVICE PARA ADJUNTOS Y AFIJOS DE M3
            if archivos_s3_raw:
                await self.s3_service.transferir_lote_s3_a_sfc(
                    sfc_client=self.sfc_client,
                    sfc_codigo_queja=sfc_id_largo,
                    adjuntos_crm=archivos_s3_raw,
                    target_file_name=target_file_name,
                    afijo_regulatorio=afijo_regulatorio,
                    afijo_masivo=afijo_masivo,
                    case_id=case_id_crm,
                    **({'ordered': True} if generar_pdf_cierre else {}),
                    **storage_kwargs
                )

            # Only after every ordinary attachment succeeded may the final response
            # be sent. Reuse a supplied file; otherwise reuse/generate via the one
            # existing generator, with a durable receipt for this closing cycle.
            if generar_pdf_cierre and close_checkpoint and not pdf_generado_exito:
                if existing_final:
                    pdf_generado_exito = True
                else:
                    try:
                        await self._generar_y_enviar_pdf_respuesta_final(
                            case_id=case_id_crm, sfc_code=sfc_id_largo,
                            cuerpo_correo_html=cuerpo_correo or "", cliente_nombre=cliente_nombre,
                            close_id=close_id, checkpoint=close_checkpoint, receipt=close_receipt,
                            replica=es_reclose, strict_persistence=es_reclose)
                    except Exception as document_error:
                        if es_reclose:
                            raise self._respuesta_final_no_enviada(document_error) from document_error
                        raise
                    pdf_generado_exito = True
                if pdf_generado_exito:
                    try:
                        stored_receipt = await close_checkpoint.obtener_respuesta_final(close_id)
                        await close_checkpoint.guardar_respuesta_final(
                            close_id, {**(stored_receipt or close_receipt or {}), 'sent': True})
                        confirmed = await close_checkpoint.obtener_respuesta_final(close_id)
                        if not self._confirmed_final_receipt(confirmed, expected_receipt, replica=es_reclose):
                            raise self._respuesta_final_no_enviada()
                    except Exception as receipt_error:
                        if es_reclose:
                            raise self._respuesta_final_no_enviada(receipt_error) from receipt_error
                        raise
                    if es_reclose:
                        self._replica_stage("REPLICA_RESP_FINAL_CHECKPOINT_CONFIRMED", close_id, status="ACCEPTED")

            if es_reclose and not pdf_generado_exito:
                raise self._respuesta_final_no_enviada()

            sfc_raw_payload["codigo_queja"] = sfc_id_largo
            sfc_raw_payload["anexo_queja"] = pdf_generado_exito or len(archivos_s3_raw) > 0
            sfc_raw_payload["fecha_actualizacion"] = datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT%H:%M:%S")

            if pdf_generado_exito:
                sfc_raw_payload["documentacion_rta_final"] = True

            self._aplicar_defaults_finales_sfc(sfc_raw_payload, afijo_regulatorio, crm_dict)

            payload_validado = SfcActualizarQuejaPayload(**sfc_raw_payload)
            payload_sfc = payload_validado.model_dump(exclude_none=True)
            # Only an explicit CRM operation clears the historical closing date.
            # All other optional nulls and all legacy requests keep their contract.
            if crm_dict.get("crm_operation") == "REOPEN":
                payload_sfc["fecha_cierre"] = None
                capture_reopen_payload(payload_sfc)

            if es_reclose:
                self._replica_stage("RECLOSE_PATCH_STARTED", close_id, status="FINAL_CONFIRMED")
            try:
                await self.sfc_client.put_actualizar_queja(
                    sfc_codigo_queja=sfc_id_largo,
                    payload=payload_sfc
                )
            except Exception:
                if es_reclose:
                    self._replica_stage("RECLOSE_PATCH_RESULT", close_id, status="ERROR")
                raise
            if es_reclose:
                self._replica_stage("RECLOSE_PATCH_RESULT", close_id, status="OK")

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
        cliente_nombre: str,
        close_id: Optional[str] = None,
        checkpoint: Optional[IdempotencyService] = None,
        receipt: Optional[dict] = None,
        replica: bool = False,
        strict_persistence: bool = False,
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

        final_pdf_name = final_response_filename(case_id, close_id or case_id, replica=replica)
        # Keep the existing legacy ownership rule (Case is the direct parent).
        s3_key = f"caso/{close_id}/{case_id}/{final_pdf_name}" if close_id else f"caso/{case_id}/{final_pdf_name}"
        if checkpoint and not receipt and not replica:
            completed = await checkpoint.obtener_archivos_completados(sfc_code, strict=True)
            legacy_key = f'caso/{case_id}/{final_pdf_name}'
            if any(key == legacy_key or key.startswith(legacy_key + ':') for key in completed):
                return  # Accepted by an older worker in this still-open closing cycle.
        if receipt and receipt.get('s3_key'):
            if receipt['s3_key'] != s3_key:
                raise ValueError('Final response checkpoint identity mismatch')
        stream = None
        if close_id:
            try:
                stream = await self.s3_service.obtener_stream_archivo(s3_key=s3_key,case_id_esperado=case_id)
            except SfcIntegrationException as error:
                if receipt or error.error_type != 'S3_FILE_NOT_FOUND':
                    raise
        if stream is not None:
            try:
                file_bytes = stream.read()
            finally:
                stream.close()
            receipt = {'s3_key':s3_key,'sent':False}
            if replica:
                self._replica_stage("REPLICA_RESP_FINAL_REUSED_CURRENT_CYCLE", close_id,
                                    file_name=final_pdf_name, status="PERSISTED")
        else:
            file_bytes = await asyncio.to_thread(_job_parsing_y_renderizado)
            if replica:
                self._replica_stage("REPLICA_RESP_FINAL_CREATED", close_id, file_name=final_pdf_name)

        if replica:
            identity = self._final_receipt_identity(case_id, sfc_code, close_id, replica=True)
            document_identity = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            bound_bytes = await asyncio.to_thread(vincular_pdf_a_ciclo, file_bytes, document_identity)
            # Upgrade only this replica, never the original final. An old `sent`
            # flag may have come from DUPLICATE_OMITTED, so it is not acceptance.
            if bound_bytes != file_bytes:
                receipt = None
            file_bytes = bound_bytes

        try:
            if not receipt:
                await self.s3_service.subir_bytes_archivo(
                    s3_key=s3_key, file_bytes=file_bytes, content_type="application/pdf")
                if checkpoint and close_id and not replica:
                    await checkpoint.guardar_respuesta_final(close_id, {'s3_key':s3_key,'sent':False})
            if replica:
                receipt = {**identity, 'version': 2, 'sha256': hashlib.sha256(file_bytes).hexdigest(), 'sent': False}
                await checkpoint.guardar_respuesta_final(close_id, receipt)
        except Exception as s3_err:
            if strict_persistence:
                raise self._respuesta_final_no_enviada(s3_err) from s3_err
            logger.error(f"⚠️ [Momento 3] No se pudo guardar la copia del PDF en S3: {s3_err}")

        if replica:
            self._replica_stage("REPLICA_RESP_FINAL_UPLOAD_STARTED", close_id, file_name=final_pdf_name)
        try:
            result = await self.s3_service.transferir_lote_s3_a_sfc(
                sfc_client=self.sfc_client,
                sfc_codigo_queja=sfc_code,
                adjuntos_crm=[{"nombre_archivo": final_pdf_name, "s3_key": s3_key, "bytes": file_bytes}],
                case_id=case_id,
                **({'require_accepted': True} if replica else {}),
            )
            if replica:
                if (len(result) != 1 or result[0].get('file_name') != final_pdf_name
                        or result[0].get('status') not in ('OK', 'ALREADY_CONFIRMED_CHECKPOINT')):
                    raise self._respuesta_final_no_enviada()
                # Preserve proof of a real upload (or its strict file receipt),
                # not merely a completed batch coroutine.
                receipt['upload_status'] = 'OK'
                await checkpoint.guardar_respuesta_final(close_id, receipt)
        except Exception as upload_error:
            if replica:
                self._replica_stage("REPLICA_RESP_FINAL_UPLOAD_RESULT", close_id,
                                    file_name=final_pdf_name, status="ERROR")
                raise self._respuesta_final_no_enviada(upload_error) from upload_error
            raise
        if replica:
            self._replica_stage("REPLICA_RESP_FINAL_UPLOAD_RESULT", close_id,
                                file_name=final_pdf_name, status=result[0]['status'])
