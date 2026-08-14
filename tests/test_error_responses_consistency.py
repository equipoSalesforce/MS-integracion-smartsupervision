# tests/test_error_responses_consistency.py
import httpx
import unittest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from app.main import app
from app.core.config import settings
from app.api.dependencies import get_sfc_client, get_s3_client
from app.core.exceptions import SfcIntegrationException
from app.services.idempotency_service import IdempotencyService
from app.services.queue_service import QueueService


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

    def _payload_valido_base(self, smart_code: str = "999000111333") -> dict:
        return {
            "Smart_Code__c": smart_code,
            "Status": "New",
            "SuppliedName": "Juan Perez",
            "SC_id_type__c": "CC",
            "id_number__c": "123456789",
            "tipo_de_persona__c": "B2C",
            "direccion__c": "Calle 123",
            "punto_recepcion": "Manual",
            "Description": "Prueba clasificación de errores",
            "smart_escalamiento_DCF__c": "No",
            "Product__c": "Wallet",
            "Categorias_COL__c": "Transacción no reconocida"
        }

    def _mockear_encolamiento(self):
        """
        Aísla la ruta de contingencia (encolar + registrar idempotencia QUEUED) de sus
        detalles internos de Redis/Lua, que ya se prueban aparte en test_cola_redis.py.
        Aquí sólo interesa: ¿el endpoint clasifica el error como contingencia y responde
        202, o lo deja propagar como si fuera un fallo de negocio del CRM?
        """
        item_encolado_mock = MagicMock()
        item_encolado_mock.id = 1
        item_encolado_mock.es_duplicado = False
        return (
            patch.object(QueueService, "encolar_despacho", new_callable=AsyncMock, return_value=item_encolado_mock),
            patch.object(IdempotencyService, "registrar_encolado", new_callable=AsyncMock),
        )

    def test_sfc_500_no_se_expone_como_error_se_encola_para_reintento(self):
        """
        P1-18: un fallo de infraestructura/dependencia upstream (SFC 500) no debe
        traducirse en un 4xx de "culpa del CRM" — se encola para reintento automático
        (202 Accepted), preservando la distinción negocio vs. infraestructura.
        """
        exc = SfcIntegrationException(
            status_code=500,
            error_type="SERVER_ERROR",
            sfc_field=None,
            raw_message="Internal Server Error de la SFC",
            crm_action="Reintente más tarde."
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=exc)

        patch_encolar, patch_idempotencia = self._mockear_encolamiento()
        with patch_encolar, patch_idempotencia:
            response = self.client.post("/api/v1/quejas/sync/despacho", json=self._payload_valido_base())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")

    def test_sfc_429_no_se_expone_como_error_se_encola_para_reintento(self):
        """P1-18: throttling de la SFC (429) es contingencia, no un error 4xx de negocio."""
        exc = SfcIntegrationException(
            status_code=429,
            error_type="THROTTLED_ERROR",
            sfc_field=None,
            raw_message="Too Many Requests",
            crm_action="Reintente más tarde."
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=exc)

        patch_encolar, patch_idempotencia = self._mockear_encolamiento()
        with patch_encolar, patch_idempotencia:
            response = self.client.post("/api/v1/quejas/sync/despacho", json=self._payload_valido_base())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")

    def test_timeout_de_red_no_se_expone_como_error_se_encola_para_reintento(self):
        """P1-18: un timeout/corte de red hacia la SFC es infraestructura, no un 4xx de negocio."""
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=httpx.TimeoutException("SFC timeout"))

        patch_encolar, patch_idempotencia = self._mockear_encolamiento()
        with patch_encolar, patch_idempotencia:
            response = self.client.post("/api/v1/quejas/sync/despacho", json=self._payload_valido_base())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")

    def test_sfc_400_negocio_no_se_encola_se_propaga_al_crm(self):
        """
        P1-18 (control): un 400 genuino de validación de negocio SÍ debe propagarse tal
        cual al CRM (nunca encolarse como si fuera infraestructura caída).
        """
        exc = SfcIntegrationException(
            status_code=400,
            error_type="VALIDATION_ERROR",
            sfc_field="Categorias_COL__c",
            raw_message="Categoría no válida",
            crm_action="Corrija la categoría en Salesforce."
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=exc)

        patch_encolar, patch_idempotencia = self._mockear_encolamiento()
        with patch_encolar as mock_encolar, patch_idempotencia:
            response = self.client.post("/api/v1/quejas/sync/despacho", json=self._payload_valido_base())

        self.assertEqual(response.status_code, 400)
        mock_encolar.assert_not_called()

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