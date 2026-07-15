# tests/test_integration_momento_2.py
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.api.dependencies import get_db, get_sfc_client, get_s3_client

class TestMomento2Integration(unittest.TestCase):

    def setUp(self):
        self.db_mock = MagicMock()
        self.sfc_client_mock = MagicMock()
        self.s3_client_mock = MagicMock()

        app.dependency_overrides[get_db] = lambda: self.db_mock
        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock
        
        self.client = TestClient(app)
        self.smart_code_test = "142316551509974606"

        # Simulación de base de datos relacional robusta (M2 + M3)[cite: 1]
        self.mock_db_response = {
            "Smart_Code__c": self.smart_code_test,
            "CreatedDate": "2026-07-14T12:00:00",
            "SuppliedName": "Camila Salas",
            "LastName": "Salas",
            "id_type__c": 1,
            "id_number__c": "1040011014",
            "canal__c": "Internet",
            "Product__c": 209,
            "Categorias_COL__c": 209,
            "Description": "Prueba de queja para validación final en integración.",
            "smart_anexo_queja__c": False,
            "Ente_de_control__c": "Otros",
            "Instancia_de_recepcion__c": 1,
            "tipo_de_persona__c": "Natural",
            "tipo_entidad": 1,
            "entidad_cod": "423",
            "SC_municipio__c": "Bogotá D.C.",
            "Departamento__c": "Bogotá",
            "admision_col__c": 1,
            
            # Campos extra del Momento 3
            "ClosedDate": "2026-07-15T10:00:00",
            "Total_Devuelto_por_Desconocimiento__c": 150000.0,
            "Aceptacion__c": True,
            "Prorroga__c": False,
            "Rectificacion__c": False,
            "sinRespuestaFinal?": True
        }

    def tearDown(self):
        app.dependency_overrides.clear()

    @patch("app.models.quejas_crud.QuejasCRUD.obtener_datos_consolidados_caso")
    @patch("app.models.quejas_crud.QuejasCRUD.actualizar_estado_caso")
    def test_endpoint_trigger_momento_2_exito(self, mock_update, mock_get_caso):
        """Verifica que el trigger use el mapper universal y Pydantic descarte campos de M3."""
        mock_get_caso.return_value = self.mock_db_response
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"status": "created"})

        payload = {"Smart_Code__c": self.smart_code_test}
        response = self.client.post("/api/v1/quejas/sync/momento-2", json=payload)
        
        self.assertEqual(response.status_code, 200)
        
        # --- VERIFICACIONES DE TRANSPORTE Y ENVOLTURA ---
        self.sfc_client_mock.post_nueva_queja.assert_called_once()
        request_enviado = self.sfc_client_mock.post_nueva_queja.call_args[0][0]
        
        # Debe contener la clave raíz 'Body'
        self.assertIn("Body", request_enviado)
        body = request_enviado["Body"]
        
        # Comprobamos los campos mapeados del Momento 2
        self.assertEqual(body["codigo_queja"], f"1423{self.smart_code_test}")
        self.assertEqual(body["canal_cod"], 13)
        self.assertEqual(body["tipo_Persona"], 1)
        
        # Los campos extra del Momento 3 deben haber sido descartados
        self.assertNotIn("fecha_cierre", body)
        self.assertNotIn("monto_reconocido", body)
        
        # El cuerpo final de salida debe tener exactamente 18 campos
        self.assertEqual(len(body), 18)

    @patch("app.models.quejas_crud.QuejasCRUD.obtener_datos_consolidados_caso")
    @patch("app.models.quejas_crud.QuejasCRUD.actualizar_estado_caso")
    def test_endpoint_trigger_momento_2_fallo_red(self, mock_update, mock_get_caso):
        """Verifica el control de errores en caso de fallo en la red de la SFC."""
        mock_get_caso.return_value = self.mock_db_response
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=Exception("Timeout en conexión con SFC"))

        payload = {"Smart_Code__c": self.smart_code_test}
        response = self.client.post("/api/v1/quejas/sync/momento-2", json=payload)
        
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("Pipeline interrumpido", data["detail"])

if __name__ == "__main__":
    unittest.main()