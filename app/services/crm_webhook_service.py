# app/services/crm_webhook_service.py
import logging
import httpx
from typing import Any, Optional, Tuple
from app.core.config import settings
from app.core.middleware import get_correlation_id
from app.core.security.sanitizer import sanitizar_headers, sanitizar_payload, sanitizar_texto_plano

logger = logging.getLogger(__name__)

_crm_client: Optional[httpx.AsyncClient] = None


def get_crm_webhook_client() -> httpx.AsyncClient:
    global _crm_client
    if _crm_client is None or _crm_client.is_closed:
        _crm_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=50)
        )
        logger.info("📡 Pool de conexiones HTTP Client para CRM Webhook inicializado.")
    return _crm_client


async def close_crm_webhook_client():
    global _crm_client
    if _crm_client and not _crm_client.is_closed:
        await _crm_client.aclose()
        _crm_client = None
        logger.info("🛑 Pool de conexiones HTTP Client para CRM Webhook liberado limpiamente.")


close_crm_fallback_client = close_crm_webhook_client


class CrmWebhookService:

    @staticmethod
    async def notificar_resolucion_contingencia(
        case_id_crm: str,
        smart_code: str,
        http_client: Optional[httpx.AsyncClient] = None
    ) -> tuple[bool, Optional[str]]:
        """
        Retorna (éxito, detalle_del_error). El detalle permite al llamador (ver
        scheduler.py) distinguir una caída transitoria de infraestructura del CRM
        (5xx/timeout/red) de un rechazo de negocio (contrato inválido, WAF,
        correlación de caso) -- una caída de infraestructura no debería consumir
        el mismo presupuesto de reintentos que un fallo real del despacho.
        """
        webhook_url = settings.CRM_WEBHOOK_URL
        api_key = settings.CRM_WEBHOOK_API_KEY

        if not webhook_url or not str(webhook_url).strip():
            logger.error(
                "❌ [CRM Webhook] Fallo de configuración: 'CRM_WEBHOOK_URL' no está definida en las "
                "variables de entorno. No se puede notificar la resolución al CRM."
            )
            return False, "Fallo de configuración: 'CRM_WEBHOOK_URL' no está definida."

        cid = get_correlation_id()

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": api_key or "",
            "X-Correlation-ID": cid,
            "User-Agent": "MS-SmartSupervision-WebhookBot/1.0"
        }

        payload = {
            "case_number": case_id_crm,
            "smart_code": smart_code,
            "status": "CREATED"
        }

        headers_clean = sanitizar_headers(headers)
        body_clean = sanitizar_payload(payload)

        # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): logger.debug() con
        # LOG_LEVEL=INFO (el valor por defecto en producción, ver logging_config.py)
        # nunca se emite -- estos logs de auditoría del webhook al CRM desaparecían por
        # completo. Mismo nivel (.info) que ya usa sfc_client.py para sus propios logs
        # AUDIT_HTTP_*.
        logger.info("AUDIT_HTTP_OUTGOING_REQUEST_CRM_WEBHOOK", extra={
            "extra_data": {
                "direction": "OUTGOING_REQUEST",
                "method": "POST",
                "url": webhook_url,
                "headers": headers_clean,
                "body": body_clean
            }
        })

        client = http_client or get_crm_webhook_client()

        try:
            response = await client.post(webhook_url, json=payload, headers=headers, timeout=10.0)

            res_headers_clean = sanitizar_headers(dict(response.headers))
            content_type = response.headers.get("content-type", "").lower()
            
            raw_json = None
            res_body_clean = None

            if "application/json" in content_type:
                try:
                    raw_json = response.json()
                    res_body_clean = sanitizar_payload(raw_json)
                except Exception:
                    # 🟡 FIX P1-16: JSON malformado igual puede reflejar datos del
                    # request original — no se loggea el texto completo sin control.
                    res_body_clean = sanitizar_texto_plano(response.text)
            else:
                # 🟡 FIX P1-16: respuesta no-JSON de un sistema externo (CRM); mismo
                # criterio que en sfc_client.py: tamaño + vista previa acotada.
                res_body_clean = sanitizar_texto_plano(response.text)

            logger.info("AUDIT_HTTP_INCOMING_RESPONSE_CRM_WEBHOOK", extra={
                "extra_data": {
                    "direction": "INCOMING_RESPONSE",
                    "status_code": response.status_code,
                    "reason_phrase": response.reason_phrase,
                    "url": webhook_url,
                    "headers": res_headers_clean,
                    "body": res_body_clean
                }
            })

            if response.status_code in (200, 201, 202):
                return CrmWebhookService._validar_respuesta_exitosa(response, content_type, raw_json, case_id_crm)
            else:
                logger.warning(
                    f"⚠️ [CRM Webhook] El CRM respondió con código HTTP de error {response.status_code}."
                )
                return False, f"El CRM respondió con código HTTP de error {response.status_code}."

        except Exception as exc:
            logger.error(f"❌ [CRM Webhook] Fallo de red/comunicación al notificar al CRM: {str(exc)}")
            return False, f"Fallo de red/comunicación al notificar al CRM: {str(exc)}"

    @staticmethod
    def _validar_respuesta_exitosa(
        response: httpx.Response, content_type: str, raw_json: Any, case_id_crm: str
    ) -> Tuple[bool, Optional[str]]:
        # 🟢 FIX HALLAZGO 14: Validación estricta de Content-Type JSON
        if "application/json" not in content_type:
            logger.warning(
                f"⚠️ [CRM Webhook] El CRM respondió HTTP {response.status_code} pero el "
                f"Content-Type no es 'application/json' (recibido: '{content_type}'). "
                "Rechazando respuesta por posible interceptación de Proxy, WAF o página HTML."
            )
            return False, (
                f"El CRM respondió HTTP {response.status_code} con Content-Type "
                f"inesperado ('{content_type}'), posible interceptación de Proxy/WAF."
            )

        # 🟢 FIX HALLAZGO 14: Validación de parseo estricto de estructura JSON
        if raw_json is None:
            try:
                raw_json = response.json()
            except Exception as json_err:
                logger.warning(
                    f"⚠️ [CRM Webhook] El CRM devolvió HTTP {response.status_code} pero falló "
                    f"la decodificación del JSON: {json_err}"
                )
                return False, (
                    f"El CRM respondió HTTP {response.status_code} pero el JSON no se "
                    f"pudo decodificar: {json_err}"
                )

        if not isinstance(raw_json, dict):
            logger.warning(
                f"⚠️ [CRM Webhook] La respuesta JSON del CRM no es un objeto válido "
                f"(recibido tipo: {type(raw_json).__name__})."
            )
            return False, (
                f"La respuesta JSON del CRM no es un objeto válido "
                f"(recibido tipo: {type(raw_json).__name__})."
            )

        # 🟢 FIX HALLAZGO 14 / P0-07: Se exige éxito EXPLÍCITO (success === true), no
        # sólo "no es false". Un {}, un {"error": "..."} o cualquier JSON sin el campo
        # 'success' ya no se aceptan como éxito (antes pasaban porque `.get(...) is
        # False` es False para None).
        if raw_json.get("success") is not True:
            logger.warning(
                f"⚠️ [CRM Webhook] El CRM devolvió HTTP {response.status_code} pero la "
                f"respuesta no cumple el contrato esperado (se requiere 'success': true "
                f"explícito; recibido: {raw_json.get('success')!r})."
            )
            return False, (
                f"El CRM no confirmó éxito explícito (success={raw_json.get('success')!r})."
            )

        # 🟢 FIX P0-07: Correlación de caso — la confirmación debe corresponder al MISMO
        # caso notificado (el mismo 'case_number' que se envió), no sólo cualquier
        # success=true. Evita aceptar como éxito una respuesta cruzada de otro caso.
        crm_case_number = raw_json.get("case_number")
        if crm_case_number != case_id_crm:
            logger.warning(
                f"⚠️ [CRM Webhook] El CRM confirmó éxito pero para un caso distinto al "
                f"notificado (enviado: {case_id_crm!r}, confirmado: {crm_case_number!r}). "
                "Rechazando por falta de correlación."
            )
            return False, (
                f"El CRM confirmó éxito para un caso distinto al notificado "
                f"(enviado={case_id_crm!r}, confirmado={crm_case_number!r})."
            )

        crm_case_id = raw_json.get("case_id", "N/A")
        is_idempotent = raw_json.get("idempotent", False)
        is_reconciled = raw_json.get("reconciled", False)

        logger.info(
            f"✅ [CRM Webhook] Confirmación recibida con éxito y contrato JSON validado. "
            f"Case Number: {crm_case_number} | Case ID: {crm_case_id} | "
            f"Idempotent: {is_idempotent} | Reconciled: {is_reconciled}"
        )
        return True, None