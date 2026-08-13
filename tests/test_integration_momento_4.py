# tests/test_integration_momento_4.py
import unittest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from app.main import app
from app.api.dependencies import get_sfc_client, verificar_api_key_crm
from app.core.exceptions import SfcIntegrationException
from app.core.config import settings


class TestMomento4Integration(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)
        self.mock_sfc_client = AsyncMock()

        app.dependency_overrides[get_sfc_client] = lambda: self.mock_sfc_client
        app.dependency_overrides[verificar_api_key_crm] = lambda: True

        self.base_url = f"{settings.API_V1_STR}/quejas"

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_endpoint_get_sync_momento_4_exitoso(self):
        """Prueba HTTP GET /sync/momento-4 retornando diccionario estructurado con usuarios mapeados."""
        mock_response = {
            "Response": {
                "results": [
                    {
                        "numero_id_CF": "987654321",
                        "tipo_id_CF": 1,
                        "nombres": "Carlos",
                        "apellidos": "Lopez",
                        "correo": "carlos@global66.com"
                    }
                ]
            }
        }
        self.mock_sfc_client.fetch_usuarios_pagina.return_value = mock_response

        response = self.client.get(f"{self.base_url}/sync/momento-4")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # 🟢 FIX: Se aserta sobre la estructura de diccionario de Momento 4
        self.assertIsInstance(data, dict)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["total_exitosos"], 1)
        self.assertEqual(len(data["usuarios"]), 1)
        self.assertEqual(data["usuarios"][0]["id_number__c"], "987654321")

    def test_endpoint_post_sync_momento_4_ack_exitoso(self):
        """Prueba HTTP POST /sync/momento-4/ack confirmando el recibido de usuarios."""
        self.mock_sfc_client.send_user_ack_batch.return_value = {
            "Response": {"message": "ID CF actualizado", "numero_id_CF_error": []}
        }

        payload = {
            "numeros_id_cf": ["987654321", "123456789"]
        }

        response = self.client.post(f"{self.base_url}/sync/momento-4/ack", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["confirmados"], 2)

    def test_endpoint_get_sync_momento_4_falla_sfc(self):
        """Valida el manejo traducido cuando la SFC devuelve una excepción controlada."""
        exc = SfcIntegrationException(
            status_code=401,
            error_type="AUTH_ERROR",
            sfc_field=None,
            raw_message="Token expirado",
            crm_action="Renovar token"
        )
        self.mock_sfc_client.fetch_usuarios_pagina.side_effect = exc

        response = self.client.get(f"{self.base_url}/sync/momento-4")

        self.assertEqual(response.status_code, 401)
        detail = response.json()
        self.assertEqual(detail["error_type"], "AUTH_ERROR")
        self.assertEqual(detail["raw_message"], "Token expirado")


if __name__ == "__main__":
    unittest.main()