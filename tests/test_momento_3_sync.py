# tests/test_momento_3_sync.py
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date
from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.api.dependencies import get_sfc_client, get_s3_client
from app.services.momento_3_sync import Momento3SincronizacionService
from app.core.exceptions import SfcIntegrationException

class TestMomento3UnitAndIntegration(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # 🪐 Mocks de Infraestructura Externa
        self.sfc_client_mock = MagicMock()
        self.s3_client_mock = MagicMock()
        
        # Inyección de Dependencias nativa para los tests de Integración (Stateless)
        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock
        
        self.client = TestClient(app)
        self.smart_code_test = "16551509974609"
        self.sfc_id_largo_test = f"1423{self.smart_code_test}"

        # 📄 Base de proforma simulada para la SFC (Equivalente al catálogo de salida del Mapper)
        self.mock_mapper_response = {
            "canal_cod": 13,          # "Internet" -> 13
            "producto_cod": 207,       # "Cuenta perfil" -> Wildcard canónico 207
            "macro_motivo_cod": 940,   # "Transacción no reconocida" -> 940
        }

    def tearDown(self):
        app.dependency_overrides.clear()

    # ======================================================================
    # 🧪 SUITE 1: PRUEBAS DE INTEGRACIÓN (HTTP ENDPOINTS & PYDANTIC)
    # ======================================================================

    def test_endpoint_cierre_fallo_pydantic_sin_archivos(self):
        """Verifica que el endpoint de cierre rechace la petición si no se envían adjuntos."""
        payload_invalido = {
            "Smart_Code__c": self.smart_code_test,
            "Status": "Closed",                           
            "canal__c": "Internet",
            "Product__c": "Cuenta perfil",                
            "Categorias_COL__c": "Transacción no reconocida", 
            "ClosedDate": "2026-07-16",
            "Favorabilidad__c": "Favorable",              
            "Aceptacion__c": "Si",                        
            "Rectificacion__c": False,                     
            "Prorroga__c": False,                          
            "archivos_s3": []  # 🚨 LISTA VACÍA: Gatilla el ValueError de negocio
        }
        
        response = self.client.put("/api/v1/quejas/sync/momento-3/cierre", json=payload_invalido)
        
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn("No se envió un documento de cierre del caso", response.text)

    def test_endpoint_fraude_fallo_pydantic_ambiguedad_archivos(self):
        """Verifica el rechazo si vienen múltiples archivos pero no se especifica el principal."""
        payload_ambiguo = {
            "Smart_Code__c": self.smart_code_test,
            "Status": "In Progress",                      
            "Product__c": "Cuenta perfil",
            "Categorias_COL__c": "Transacción no reconocida",
            "tipo_fraude__c": "Interno",                  
            "modalidad_fraude__c": "Vulneración de cuenta o producto", 
            "monto_reclamado__c": 50000.0,                
            "monto_reconocido__c": 0.0,                   
            "nombre_archivo_fraude": None,                # 🚨 AMBIGÜEDAD
            "archivos_s3": [
                {"nombre_archivo": "soporte1.pdf", "s3_key": "k1", "bucket": "b1"},
                {"nombre_archivo": "soporte2.xlsx", "s3_key": "k2", "bucket": "b1"}
            ]
        }
        
        response = self.client.put("/api/v1/quejas/sync/momento-3/fraude", json=payload_ambiguo)
        
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn("no se encuentra dentro del listado de archivos_s3", response.text)

    @patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload")
    def test_endpoint_tramite_exito_stateless(self, mock_mapper):
        """Prueba de punta a punta de una actualización rutinaria sin adjuntos."""
        sfc_mock = self.mock_mapper_response.copy()
        sfc_mock["estado_cod"] = 2                        
        mock_mapper.return_value = sfc_mock
        
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"status": "updated"})

        payload_tramite = {
            "Smart_Code__c": self.smart_code_test,
            "Status": "In Progress",                      
            "canal__c": "Internet",
            "Product__c": "Cuenta perfil",                
            "Categorias_COL__c": "Transacción no reconocida", 
            "producto_digital__c": "Si",                  # 🎯 CORREGIDO: Cambiado de 1 a "Si" (String Picklist)
            "admision_col__c": "Queja o reclamo admitida por el DCF", 
            "archivos_s3": []
        }

        response = self.client.put("/api/v1/quejas/sync/momento-3/tramite", json=payload_tramite)
        
        # Control de aserción informativo en caso de fallos
        self.assertEqual(response.status_code, status.HTTP_200_OK, msg=f"Fallo en esquema: {response.text}")
        self.sfc_client_mock.put_actualizar_queja.assert_called_once()

    # ======================================================================
    # 🧪 SUITE 2: PRUEBAS UNITARIAS (LÓGICA CORE DEL SERVICIO & S3)
    # ======================================================================

    @patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload")
    async def test_servicio_cierre_autoasignacion_y_renombrado_un_solo_archivo(self, mock_mapper):
        """Verifica que si viene un solo archivo, se autoasigne y renombre con RESP_FINAL_SFC."""
        sfc_mock = self.mock_mapper_response.copy()
        sfc_mock["estado_cod"] = 4                        
        sfc_mock["fecha_cierre"] = "2026-07-16"           
        sfc_mock["a_favor_de"] = 1                        
        sfc_mock["aceptacion_queja"] = 1                  
        sfc_mock["rectificacion_queja"] = 2               
        sfc_mock["prorroga_queja"] = 2                    
        mock_mapper.return_value = sfc_mock
        
        self.sfc_client_mock.post_adjunto_queja = AsyncMock(return_value={"id": 99})
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"status": "closed"})
        
        self.s3_client_mock.head_object = MagicMock(return_value={"ContentLength": 1024})
        mock_body = MagicMock()
        mock_body.read = MagicMock(return_value=b"bytes_pdf_cierre")
        self.s3_client_mock.get_object = MagicMock(return_value={"Body": mock_body})

        servicio = Momento3SincronizacionService(sfc_client=self.sfc_client_mock, s3_client=self.s3_client_mock)
        
        from app.schemas.crm_payloads import Momento3CierreCrmInput
        input_pydantic = Momento3CierreCrmInput(
            Smart_Code__c=self.smart_code_test,
            Status="Closed",                      
            canal__c="Internet",
            Product__c="Cuenta perfil",                  
            Categorias_COL__c="Transacción no reconocida", 
            ClosedDate=date(2026, 7, 16),
            Favorabilidad__c="Favorable",     
            a_favor_de__c=1,
            Aceptacion__c="Si",                   
            Rectificacion__c=False,
            Prorroga__c=False,
            nombre_archivo_final=None, 
            archivos_s3=[
                {"nombre_archivo": "resolución_final.pdf", "s3_key": "path/resolucion.pdf", "bucket": "global-bucket"}
            ]
        )

        resultado = await servicio.ejecutar_cierre_definitivo(payload=input_pydantic)

        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_adjunto_queja.assert_called_once()
        kwargs_archivo = self.sfc_client_mock.post_adjunto_queja.call_args[1]
        
        self.assertEqual(kwargs_archivo["file_name"], "resolución_final_RESP_FINAL_SFC.pdf")
        self.assertEqual(kwargs_archivo["sfc_codigo_queja"], self.sfc_id_largo_test)

        self.sfc_client_mock.put_actualizar_queja.assert_called_once()
        payload_formulario_sfc = self.sfc_client_mock.put_actualizar_queja.call_args[1]["payload"]
        
        self.assertEqual(payload_formulario_sfc["estado_cod"], 4) 
        self.assertEqual(payload_formulario_sfc["fecha_cierre"], "2026-07-16")
        self.assertTrue(payload_formulario_sfc["anexo_queja"])

    @patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload")
    async def test_servicio_aislamiento_de_fallas_sfc_exception(self, mock_mapper):
        """Verifica que si la SFC reconoce la falla, la excepción controlada suba intacta."""
        sfc_mock = self.mock_mapper_response.copy()
        sfc_mock["estado_cod"] = 2
        mock_mapper.return_value = sfc_mock
        
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(
            side_effect=SfcIntegrationException(
                status_code=400,
                error_type="VALIDATION_ERROR",
                sfc_field="estado_cod",
                raw_message="El código de estado no es válido",
                crm_action="Validar flujo de estados"
            )
        )

        servicio = Momento3SincronizacionService(sfc_client=self.sfc_client_mock, s3_client=None)
        
        from app.schemas.crm_payloads import Momento3TramiteCrmInput
        input_tramite = Momento3TramiteCrmInput(
            Smart_Code__c=self.smart_code_test,
            Status="In Progress",                  
            canal__c="Internet",
            Product__c="Cuenta perfil",                  
            Categorias_COL__c="Transacción no reconocida", 
            producto_digital__c="Si",
            admision_col__c="Queja o reclamo admitida por el DCF",
            archivos_s3=[]
        )

        with self.assertRaises(SfcIntegrationException):
            await servicio.ejecutar_actualizacion_tramite(payload=input_tramite)

if __name__ == "__main__":
    unittest.main()