import unittest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.api.dependencies import get_sfc_client, get_s3_client

class TestMomento2Integration(unittest.TestCase):

    def setUp(self):
        self.sfc_client_mock = MagicMock()
        self.s3_client_mock = MagicMock()

        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock
        
        self.client = TestClient(app)
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

        self.smart_code_test = "16551509974606"

        # 📄 Payload representativo de CREACIÓN PURA M2 (Campos de M3 en None)
        self.mock_crm_payload = {
            "Smart_Code__c": self.smart_code_test,
            "CreatedDate": "2026-07-14T12:00:00",
            "Status": "New",
            "status": "New",
            "SuppliedName": "Camila Salas",
            "SC_id_type__c": "CC",
            "id_number__c": "1040011014",
            "sc_genero__c": None,               # 👈 None para M2 Puro
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": None,               # 👈 None para M2 Puro
            "sc_Condicion_especial__c": None,   # 👈 None para M2 Puro
            "producto_digital__c": None,        # 👈 None para M2 Puro
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "camila@test.com",
            "direccion__c": "Calle 93 # 11-11",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "canal__c": "Internet",
            "punto_recepcion": "Manual",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "admision_col__c": "No Aplica",
            "Description": "Prueba de queja para validación final en integración.",
            "smart_anexo_queja__c": False,
            "Tutela__c": "No",
            "Ente_de_control__c": "Otros",
            "smart_escalamiento_DCF__c": "No",
            "Product__c": "Cuenta perfil",
            "smart_Producto_nombre__c": "Ahorro",
            "Categorias_COL__c": "Transacción no reconocida",
            "archivos_s3": []
        }

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_endpoint_despacho_momento_2_exito(self):
        """Verifica que el despacho unificado enrute exitosamente una queja nueva al Momento 2."""
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"status": "created"})
        self.sfc_client_mock.put_actualizar_queja = AsyncMock()

        response = self.client.post("/api/v1/quejas/sync/despacho", json=self.mock_crm_payload)
        
        self.assertEqual(response.status_code, 200)
        self.sfc_client_mock.post_nueva_queja.assert_called_once()
        
        request_enviado = self.sfc_client_mock.post_nueva_queja.call_args[0][0]
        self.assertEqual(request_enviado["codigo_queja"], f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}{self.smart_code_test}")
        self.assertEqual(request_enviado["canal_cod"], 13)
        self.assertEqual(request_enviado["tipo_persona"], 1)

    def test_endpoint_despacho_momento_2_fallo_red(self):
        """Verifica el control de errores en caso de fallo en la red de la SFC al crear queja."""
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=Exception("Timeout en conexión con SFC"))
        self.sfc_client_mock.put_actualizar_queja = AsyncMock()

        response = self.client.post("/api/v1/quejas/sync/despacho", json=self.mock_crm_payload)
        
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("Timeout en conexión con SFC", data["raw_message"])


if __name__ == "__main__":
    unittest.main()