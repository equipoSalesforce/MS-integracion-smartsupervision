# tests/test_integration_momento_2.py
import unittest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings  # 👈 Importación requerida para la API Key
from app.api.dependencies import get_sfc_client, get_s3_client

class TestMomento2Integration(unittest.TestCase):

    def setUp(self):
        # Inicializamos los mocks de los clientes externos de infraestructura
        self.sfc_client_mock = MagicMock()
        self.s3_client_mock = MagicMock()

        # Inyectamos los overrides de FastAPI para los clientes de salida
        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock
        
        self.client = TestClient(app)
        
        # 🎯 INYECCIÓN DE API KEY: Permite al cliente pasar el header de seguridad de FastAPI
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

        self.smart_code_test = "16551509974606"  # Código CRM limpio sin el prefijo 1423

        # 📄 Payload representativo que el CRM le envía directamente al endpoint por HTTP
        self.mock_crm_payload = {
            "Smart_Code__c": self.smart_code_test,
            "CreatedDate": "2026-07-14T12:00:00",
            "Status": "New",
            "SuppliedName": "Camila Salas",
            "SC_id_type__c": "CC",
            "id_number__c": "1040011014",
            "sc_genero__c": "Femenino",
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": "No",
            "sc_Condicion_especial__c": "No aplica",
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
            "archivos_s3": [],
            
            # Campos extra del Momento 3 que Pydantic debe ignorar/descartar en M2
            "ClosedDate": "2026-07-15T10:00:00",
            "Total_Devuelto_por_Desconocimiento__c": 150000.0,
            "Aceptacion__c": True,
            "Prorroga__c": False,
            "Rectificacion__c": False,
            "sinRespuestaFinal?": True
        }

    def tearDown(self):
        # Limpieza crucial para no contaminar otros archivos de pruebas
        app.dependency_overrides.clear()

    def test_endpoint_trigger_momento_2_exito(self):
        """Verifica que el trigger use el mapper universal y Pydantic descarte campos de M3."""
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"status": "created"})

        # Enviamos el JSON completo imitando al CRM real
        response = self.client.post("/api/v1/quejas/sync/momento-2", json=self.mock_crm_payload)
        
        self.assertEqual(response.status_code, 200)
        
        # --- VERIFICACIONES DE TRANSPORTE ---
        self.sfc_client_mock.post_nueva_queja.assert_called_once()
        
        # 🎯 CAPTURA DIRECTA: El request enviado es el diccionario plano interceptado
        request_enviado = self.sfc_client_mock.post_nueva_queja.call_args[0][0]
        
        # Comprobamos los campos mapeados correctamente al formato SFC directamente
        self.assertEqual(request_enviado["codigo_queja"], f"1423{self.smart_code_test}")
        self.assertEqual(request_enviado["canal_cod"], 13)
        self.assertEqual(request_enviado["tipo_Persona"], 1)
        
        # Los campos extra del Momento 3 deben haber sido descartados automáticamente por Pydantic
        self.assertNotIn("fecha_cierre", request_enviado)
        self.assertNotIn("monto_reconocido", request_enviado)

    def test_endpoint_trigger_momento_2_fallo_red(self):
        """Verifica el control de errores en caso de fallo en la red de la SFC."""
        # Simulamos una caída de red o timeout con la SFC
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=Exception("Timeout en conexión con SFC"))

        # Enviamos el payload al endpoint
        response = self.client.post("/api/v1/quejas/sync/momento-2", json=self.mock_crm_payload)
        
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("Pipeline interrumpido", data["detail"])

if __name__ == "__main__":
    unittest.main()