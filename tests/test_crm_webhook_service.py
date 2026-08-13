# tests/test_crm_webhook_service.py
import unittest
from unittest.mock import AsyncMock, MagicMock
from app.services.crm_webhook_service import CrmWebhookService


class TestCrmWebhookServiceContractValidation(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_http_client = AsyncMock()

    async def test_200_ok_html_waf_response_rejected(self):
        """Verifica que un HTTP 200 con cuerpo HTML (ej. WAF/Proxy) sea rechazado."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html; charset=utf-8"}
        mock_response.text = "<html><body>502 Bad Gateway / WAF Challenge</body></html>"
        mock_response.json.side_effect = Exception("Not JSON")

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        resultado = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(resultado)

    async def test_200_ok_json_success_false_rejected(self):
        """Verifica que un HTTP 200 con JSON pero 'success': false sea rechazado."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"success": False, "error": "Caso bloqueado"}

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        resultado = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(resultado)

    async def test_200_ok_json_valid_contract_accepted(self):
        """Verifica que un HTTP 200 con JSON y contrato válido sea aceptado."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {
            "success": True,
            "case_id": "50000000001",
            "case_number": "CASE-001",
            "idempotent": False
        }

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        resultado = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertTrue(resultado)


if __name__ == "__main__":
    unittest.main()