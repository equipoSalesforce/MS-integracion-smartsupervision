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
    """
    Obtiene o inicializa el cliente HTTP asíncrono persistente para notificaciones al CRM.
    Mantiene el pool de conexiones (Keep-Alive) abierto durante toda la vida de la app.
    """
    global _crm_client
    if _crm_client is None or _crm_client.is_closed:
        _crm_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=50)
        )
        logger.info("📡 Pool de conexiones HTTP Client para CRM Webhook inicializado.")
    return _crm_client


async def close_crm_webhook_client():
    """Cierra limpiamente el pool de conexiones del CRM Webhook al apagar el microservicio."""
    global _crm_client
    if _crm_client and not _crm_client.is_closed:
        await _crm_client.aclose()
        _crm_client = None
        logger.info("🛑 Pool de conexiones HTTP Client para CRM Webhook liberado limpiamente.")


close_crm_fallback_client = close_crm_webhook_client


class CrmWebhookService:
    """
    Servicio encargado de notificar al CRM/Salesforce únicamente cuando un caso
    ha sido creado/actualizado con éxito en la SFC.
    """

    @staticmethod
    async def notificar_creacion_exitosa(
        case_id_crm: str, 
        smart_code: str,
        http_client: Optional[httpx.AsyncClient] = None
    ) -> bool:
        webhook_url = settings.CRM_WEBHOOK_URL
        api_key = settings.CRM_WEBHOOK_API_KEY

        if not webhook_url:
            logger.info("ℹ️ [CRM Webhook] CRM_WEBHOOK_URL no configurada. Omitiendo notificación.")
            # 🟢 Si no está configurada la URL, no se requiere notificación Webhook.
            # Retorna True para no bloquear el estado EXITOSO en la cola de Redis.
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
        headers_formatted = "\n".join([f"   {k}: {v}" for k, v in headers_clean.items()])
        body_str = json.dumps(sanitizar_payload(payload), ensure_ascii=False)

        logger.info(
            "\n==================== [AUDIT HTTP OUTGOING REQUEST (CRM WEBHOOK)] ====================\n"
            f"Correlation-ID : {cid}\n"
            f"Method         : POST\n"
            f"URL            : {webhook_url}\n"
            f"Headers :\n{headers_formatted}\n"
            f"Body           :\n{body_str}\n"
            "=========================================================================="
        )

        client = http_client or get_crm_webhook_client()

        try:
            response = await client.post(webhook_url, json=payload, headers=headers, timeout=10.0)

            res_headers_clean = sanitizar_headers(dict(response.headers))
            res_headers_formatted = "\n".join([f"   {k}: {v}" for k, v in res_headers_clean.items()])
            
            try:
                raw_json = response.json() if response.text else {}
                res_body_str = json.dumps(sanitizar_payload(raw_json), ensure_ascii=False)
            except Exception:
                res_body_str = response.text or "<Vacio>"

            logger.info(
                "\n==================== [AUDIT HTTP INCOMING RESPONSE (CRM WEBHOOK)] ====================\n"
                f"Correlation-ID : {cid}\n"
                f"Status         : {response.status_code} {response.reason_phrase}\n"
                f"URL            : {webhook_url}\n"
                f"Headers :\n{res_headers_formatted}\n"
                f"Body           :\n{res_body_str}\n"
                "=========================================================================="
            )

            if response.status_code in (200, 201, 202):
                data = response.json() if response.text else {}
                logger.info(
                    f"✅ [CRM Webhook] [CID: {cid}] Confirmación recibida por el CRM. "
                    f"Case ID: {data.get('case_id', 'N/A')}"
                )
                return True
            else:
                logger.warning(
                    f"⚠️ [CRM Webhook] [CID: {cid}] El CRM respondió con código {response.status_code}: {response.text}"
                )
                return False

        except Exception as exc:
            logger.error(f"❌ [CRM Webhook] [CID: {cid}] Fallo de red/comunicación al notificar al CRM: {str(exc)}")
            return False