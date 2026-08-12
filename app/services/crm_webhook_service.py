# app/services/crm_webhook_service.py
import json
import logging
import httpx
from typing import Optional
from app.core.config import settings
from app.core.middleware import get_correlation_id
from app.core.security.sanitizer import sanitizar_headers, sanitizar_payload

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
    ) -> bool:
        webhook_url = settings.CRM_WEBHOOK_URL
        api_key = settings.CRM_WEBHOOK_API_KEY

        if not webhook_url:
            logger.info("ℹ️ [CRM Webhook] CRM_WEBHOOK_URL no configurada. Omitiendo notificación.")
            return True

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

        # 🟢 FIX: Auditoría HTTP estructurada en JSON sin saltos de línea \n
        logger.debug("AUDIT_HTTP_OUTGOING_REQUEST_CRM_WEBHOOK", extra={
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
            raw_json = {}
            try:
                raw_json = response.json() if response.text else {}
                res_body_clean = sanitizar_payload(raw_json)
            except Exception:
                res_body_clean = response.text or None

            logger.debug("AUDIT_HTTP_INCOMING_RESPONSE_CRM_WEBHOOK", extra={
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
                is_success = raw_json.get("success", True) if isinstance(raw_json, dict) else True
                
                if is_success:
                    crm_case_id = raw_json.get("case_id", "N/A")
                    crm_case_number = raw_json.get("case_number", case_id_crm)
                    is_idempotent = raw_json.get("idempotent", False)
                    is_reconciled = raw_json.get("reconciled", False)

                    logger.info(
                        f"✅ [CRM Webhook] Confirmación recibida por el CRM con éxito. "
                        f"Case Number: {crm_case_number} | Case ID: {crm_case_id} | "
                        f"Idempotent: {is_idempotent} | Reconciled: {is_reconciled}"
                    )
                    return True
                else:
                    logger.warning(
                        f"⚠️ [CRM Webhook] El CRM respondió HTTP {response.status_code} pero indicó 'success': false."
                    )
                    return False
            else:
                logger.warning(
                    f"⚠️ [CRM Webhook] El CRM respondió con código {response.status_code}."
                )
                return False

        except Exception as exc:
            logger.error(f"❌ [CRM Webhook] Fallo de red/comunicación al notificar al CRM: {str(exc)}")
            return False