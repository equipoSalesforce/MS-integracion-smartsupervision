# tests/test_crm_webhook_service.py
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
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

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(exito)
        self.assertIsNotNone(detalle)

    async def test_200_ok_json_success_false_rejected(self):
        """Verifica que un HTTP 200 con JSON pero 'success': false sea rechazado."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"success": False, "error": "Caso bloqueado"}

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(exito)
        self.assertIsNotNone(detalle)

    async def test_200_ok_json_empty_body_rejected(self):
        """🟢 FIX P0-07: Un HTTP 200 con JSON vacío {} ya no debe aceptarse como éxito."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {}

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(exito)
        self.assertIsNotNone(detalle)

    async def test_200_ok_json_success_true_case_number_incorrecto_rejected(self):
        """🟢 FIX P0-07: success=true para un case_number distinto al notificado debe rechazarse."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {
            "success": True,
            "case_id": "50000000001",
            "case_number": "CASE-999-OTRO-CASO",
        }

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(exito)
        self.assertIsNotNone(detalle)

    async def test_case_id_crm_ausente_con_case_number_tambien_ausente_no_pasa_trivialmente(self):
        """
        🔴 FIX (hallazgo propio, 2026-08-27): sin el `not case_id_crm`, un case_id_crm
        vacío/None coincidiría trivialmente con un 'case_number' ausente en la
        respuesta del CRM (None != None -> False), pasando por alto por completo la
        protección de correlación (hallazgo 14/P0-07). Defensa en profundidad: aunque
        hoy scheduler.py siempre resuelve un Case_id/smart_code no vacío antes de
        llamar aquí, esta validación no debe depender de esa garantía externa.
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"success": True, "case_id": "50000000001"}

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm=None,
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(exito)
        self.assertIsNotNone(detalle)

    async def test_200_ok_content_type_json_pero_cuerpo_no_es_un_objeto_rejected(self):
        """Un array JSON top-level (`[1,2,3]`) o cualquier tipo no-dict pasa el
        chequeo de Content-Type pero debe rechazarse igual -- `raw_json.get(...)`
        asumiría un dict."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = [1, 2, 3]

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(exito)
        self.assertIsNotNone(detalle)

    async def test_200_ok_content_type_json_pero_cuerpo_json_malformado_rejected(self):
        """Content-Type dice 'application/json' pero el cuerpo no parsea (JSON
        truncado/corrupto) -- no debe crashear, debe rechazarse con detalle."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.side_effect = ValueError("Expecting value: line 1 column 1")
        mock_response.text = '{"success": tru'

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(exito)
        self.assertIsNotNone(detalle)

    async def test_200_ok_json_success_truthy_no_estrictamente_true_rejected(self):
        """🟢 FIX P0-07: se exige `success is True` estricto -- un valor "truthy"
        pero no exactamente `True` (string "true", o 1) debe rechazarse igual que
        `False` o ausente."""
        for valor_success in ("true", 1, "yes"):
            with self.subTest(valor_success=valor_success):
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.headers = {"content-type": "application/json"}
                mock_response.json.return_value = {
                    "success": valor_success, "case_number": "CASE-001"
                }
                self.mock_http_client.post = AsyncMock(return_value=mock_response)

                exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
                    case_id_crm="CASE-001",
                    smart_code="1286SC001",
                    http_client=self.mock_http_client
                )

                self.assertFalse(exito)
                self.assertIsNotNone(detalle)

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

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertTrue(exito)
        self.assertIsNone(detalle)

    async def test_logs_de_auditoria_se_emiten_en_nivel_info(self):
        """
        🔴 FIX (hallazgo de revisión externa, 2026-08-25): los logs
        AUDIT_HTTP_*_CRM_WEBHOOK usaban logger.debug() -- con LOG_LEVEL=INFO (el
        valor por defecto en producción, ver logging_config.py), nunca se emitían.
        El rastro de auditoría del webhook al CRM desaparecía por completo.
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {
            "success": True, "case_id": "50000000001", "case_number": "CASE-001", "idempotent": False
        }
        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        with self.assertLogs("app.services.crm_webhook_service", level="INFO") as logs:
            await CrmWebhookService.notificar_resolucion_contingencia(
                case_id_crm="CASE-001", smart_code="1286SC001", http_client=self.mock_http_client
            )

        mensajes = [r.getMessage() for r in logs.records]
        self.assertIn("AUDIT_HTTP_OUTGOING_REQUEST_CRM_WEBHOOK", mensajes)
        self.assertIn("AUDIT_HTTP_INCOMING_RESPONSE_CRM_WEBHOOK", mensajes)

    async def test_503_service_unavailable_detalle_permite_clasificar_como_infraestructura(self):
        """
        El detalle retornado en una caída de infraestructura del CRM (5xx) debe
        contener el código HTTP -- scheduler.py lo usa vía _es_falla_infraestructura
        para decidir si el fallo consume intento de la cola o no.
        """
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"error": "Service Unavailable"}

        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(exito)
        self.assertIn("503", detalle)

    async def test_fallo_de_red_detalle_permite_clasificar_como_infraestructura(self):
        self.mock_http_client.post = AsyncMock(side_effect=Exception("Connection refused"))

        exito, detalle = await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001",
            smart_code="1286SC001",
            http_client=self.mock_http_client
        )

        self.assertFalse(exito)
        self.assertIn("Connection refused", detalle)


class TestCrmWebhookServiceMetricaEmf(unittest.IsolatedAsyncioTestCase):
    """Métrica EMF SSV/CrmWebhookService (propuesta de observabilidad CX)."""

    def setUp(self):
        self.mock_http_client = AsyncMock()
        self.patcher_metrica = patch("app.services.crm_webhook_service.emit_emf_metric")
        self.mock_emit = self.patcher_metrica.start()
        self.addCleanup(self.patcher_metrica.stop)

    def _dimensiones_ssv(self):
        return [
            c.kwargs["dimensions"] for c in self.mock_emit.call_args_list
            if c.kwargs["namespace"] == "SSV/CrmWebhookService"
        ]

    async def test_contrato_valido_emite_metrica_resultado_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {
            "success": True, "case_id": "50000000001", "case_number": "CASE-001", "idempotent": False
        }
        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001", smart_code="1286SC001", http_client=self.mock_http_client
        )

        dims = self._dimensiones_ssv()
        self.assertEqual(len(dims), 1)
        self.assertEqual(dims[0]["resultado"], "success")

    async def test_contrato_invalido_emite_metrica_error_contract_violation(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"success": False, "error": "Caso bloqueado"}
        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001", smart_code="1286SC001", http_client=self.mock_http_client
        )

        dims = self._dimensiones_ssv()
        self.assertEqual(len(dims), 1)
        self.assertEqual(dims[0]["resultado"], "error")
        self.assertEqual(dims[0]["categoria_error"], "CONTRACT_VIOLATION")

    async def test_http_503_emite_metrica_error_con_codigo_http(self):
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"error": "Service Unavailable"}
        self.mock_http_client.post = AsyncMock(return_value=mock_response)

        await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001", smart_code="1286SC001", http_client=self.mock_http_client
        )

        dims = self._dimensiones_ssv()
        self.assertEqual(len(dims), 1)
        self.assertEqual(dims[0]["resultado"], "error")
        self.assertEqual(dims[0]["categoria_error"], "HTTP_503")

    async def test_fallo_de_red_emite_metrica_error_network_error(self):
        self.mock_http_client.post = AsyncMock(side_effect=Exception("Connection refused"))

        await CrmWebhookService.notificar_resolucion_contingencia(
            case_id_crm="CASE-001", smart_code="1286SC001", http_client=self.mock_http_client
        )

        dims = self._dimensiones_ssv()
        self.assertEqual(len(dims), 1)
        self.assertEqual(dims[0]["resultado"], "error")
        self.assertEqual(dims[0]["categoria_error"], "NETWORK_ERROR")

    async def test_webhook_url_no_configurada_emite_metrica_error_config_error(self):
        with patch("app.services.crm_webhook_service.settings.CRM_WEBHOOK_URL", ""):
            await CrmWebhookService.notificar_resolucion_contingencia(
                case_id_crm="CASE-001", smart_code="1286SC001", http_client=self.mock_http_client
            )

        dims = self._dimensiones_ssv()
        self.assertEqual(len(dims), 1)
        self.assertEqual(dims[0]["resultado"], "error")
        self.assertEqual(dims[0]["categoria_error"], "CONFIG_ERROR")


if __name__ == "__main__":
    unittest.main()
