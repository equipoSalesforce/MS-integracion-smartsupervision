# app/services/crm_webhook_service.py
import logging
import httpx
from typing import Optional
from app.core.config import settings

logger = logging.getLogger(__name__)


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
        """
        Envía un POST al CRM confirmando la radicación exitosa del caso.
        
        Payload enviado al CRM:
        {
            "case_number": "990012",
            "smart_code": "1234567890",
            "status": "CREATED"
        }
        """
        webhook_url = settings.CRM_WEBHOOK_URL
        api_key = settings.CRM_WEBHOOK_API_KEY

        if not webhook_url:
            logger.info("ℹ️ [CRM Webhook] CRM_WEBHOOK_URL no configurada. Omitiendo notificación.")
            return False

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": api_key or "",
            "User-Agent": "MS-SmartSupervision-WebhookBot/1.0"
        }

        payload = {
            "case_number": case_id_crm,
            "smart_code": smart_code,
            "status": "CREATED"
        }

        logger.info(f"📣 [CRM Webhook] Notificando creación exitosa al CRM para el caso {case_id_crm} ({smart_code})...")

        try:
            if http_client:
                response = await http_client.post(webhook_url, json=payload, headers=headers, timeout=10.0)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(webhook_url, json=payload, headers=headers)

            if response.status_code in (200, 201, 202):
                data = response.json() if response.text else {}
                logger.info(
                    f"✅ [CRM Webhook] Confirmación recibida por el CRM. "
                    f"Case ID: {data.get('case_id', 'N/A')} | Idempotent: {data.get('idempotent', True)}"
                )
                return True
            else:
                logger.warning(
                    f"⚠️ [CRM Webhook] El CRM respondió con código {response.status_code}: {response.text}"
                )
                return False

        except Exception as exc:
            logger.error(f"❌ [CRM Webhook] Fallo de red/comunicación al notificar al CRM: {str(exc)}")
            return False