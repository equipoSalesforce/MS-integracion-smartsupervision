# tests/test_error_responses_consistency.py
import unittest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from app.main import app
from app.core.config import settings
from app.api.dependencies import get_sfc_client, get_s3_client
from app.core.exceptions import SfcIntegrationException


class TestErrorResponsesConsistency(unittest.TestCase):

    def setUp(self):
        self.sfc_client_mock = MagicMock()
        self.s3_client_mock = MagicMock()

        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock

        # 🟢 SIMULACIÓN DE REDIS: Evita que el Fail-Closed bloquee con 503 durante unit tests
        self.redis_patcher = patch("app.api.routes_quejas.get_redis_client")
        self.mock_get_redis = self.redis_patcher.start()

        self.mock_redis = AsyncMock()
        self.mock_redis.get.return_value = None
        self.mock_redis.set.return_value = True
        self.mock_get_redis.return_value = self.mock_redis

        self.client = TestClient(app)
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

    def tearDown(self):
        app.dependency_overrides.clear()
        self.redis_patcher.stop()

    def _assert_canonical_error_structure(self, response_json: dict, expected_status: int):
        """Helper que aserta que la respuesta cumpla con la estructura exigida por Salesforce."""
        self.assertIn("status_code", response_json)
        self.assertIn("error_type", response_json)
        self.assertIn("sfc_field", response_json)
        self.assertIn("raw_message", response_json)
        self.assertIn("crm_action_friendly", response_json)
        self.assertEqual(response_json["status_code"], expected_status)

    def test_401_unauthorized_api_key(self):
        """Verifica el contrato de error cuando la API Key es inválida."""
        client_unauth = TestClient(app)
        response = client_unauth.get("/api/v1/quejas/sync/momento-1", headers={"X-API-Key": "LLAVE_INVALIDA"})
        
        self.assertEqual(response.status_code, 401)
        self.assertIn("detail", response.json())

    def test_400_pydantic_validation_error(self):
        """Verifica la respuesta canónica ante payload con campos inválidos de Pydantic."""
        payload_invalido = {
            "Case_id": "TEST_001",
            "id_number__c": "NO_TIENE_NUMEROS",  # Lanza ValidationError
        }
        response = self.client.post("/api/v1/quejas/sync/despacho", json=payload_invalido)
        
        self.assertEqual(response.status_code, 400)
        self._assert_canonical_error_structure(response.json(), expected_status=400)
        self.assertEqual(response.json()["error_type"], "CRM_PAYLOAD_VALIDATION_ERROR")

    def test_400_sfc_integration_exception(self):
        """Verifica la respuesta traducida de la SFC mapeada por la matriz de errores."""
        exc = SfcIntegrationException(
            status_code=400,
            error_type="VALIDATION_ERROR",
            sfc_field="Categorias_COL__c",
            raw_message="Categoría no válida para el producto seleccionado",
            crm_action="Cambiar la categoría COL en Salesforce"
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=exc)

        payload_valido = {
            "Smart_Code__c": "999000111222",
            "Status": "New",  # 🎯 Status 'New' gatilla Momento 2 puro (post_nueva_queja)
            "SuppliedName": "Juan Perez",
            "SC_id_type__c": "CC",
            "id_number__c": "123456789",
            "sc_genero__c": None,
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": None,
            "sc_Condicion_especial__c": None,
            "producto_digital__c": None,
            "direccion__c": "Calle 123",
            "punto_recepcion": "Manual",
            "Description": "Prueba de error traducido",
            "smart_escalamiento_DCF__c": "No",
            "Product__c": "Wallet",
            "Categorias_COL__c": "Transacción no reconocida"
        }

        response = self.client.post("/api/v1/quejas/sync/despacho", json=payload_valido)
        
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self._assert_canonical_error_structure(data, expected_status=400)
        self.assertEqual(data["error_type"], "VALIDATION_ERROR")
        self.assertEqual(data["sfc_field"], "Categorias_COL__c")

    @patch("app.api.routes_quejas.DespachoQuejaOrquestador.procesar_despacho")
    def test_500_unhandled_internal_exception(self, mock_procesar):
        """Verifica que un error no controlado (500) en el controlador responda con INTERNAL_SERVER_ERROR."""
        mock_procesar.side_effect = RuntimeError("Fallo crítico e inesperado no controlado")

        payload_valido = {
            "Smart_Code__c": "999000111222",
            "Status": "New",
            "SuppliedName": "Juan Perez",
            "SC_id_type__c": "CC",
            "id_number__c": "123456789",
            "tipo_de_persona__c": "B2C",
            "direccion__c": "Calle 123",
            "punto_recepcion": "Manual",
            "Description": "Prueba error 500 no controlado",
            "smart_escalamiento_DCF__c": "No",
            "Product__c": "Wallet",
            "Categorias_COL__c": "Transacción no reconocida"
        }

        client_no_raise = TestClient(app, raise_server_exceptions=False)
        client_no_raise.headers.update({"X-API-Key": settings.CRM_API_KEY})

        response = client_no_raise.post("/api/v1/quejas/sync/despacho", json=payload_valido)
        
        self.assertEqual(response.status_code, 500)
        data = response.json()
        self._assert_canonical_error_structure(data, expected_status=500)
        self.assertEqual(data["error_type"], "INTERNAL_SERVER_ERROR")


if __name__ == "__main__":
    unittest.main()