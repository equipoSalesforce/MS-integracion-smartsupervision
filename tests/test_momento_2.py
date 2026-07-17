import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.momento_2_sync import Momento2SincronizacionService
from app.integrations.sfc_client import SfcClient
from app.core.exceptions import SfcIntegrationException

class TestMomento2Pipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Mocks de dependencias
        self.sfc_client_mock = MagicMock(spec=SfcClient)
        self.s3_client_mock = MagicMock()
        
        self.smart_code = "142316551509974606"
        
        # Diccionario simulado de payload del CRM
        self.mock_datos_consolidados = {
            "Smart_Code__c": self.smart_code,
            "CreatedDate": "2026-07-14T12:00:00",
            
            "SuppliedName": "Camila Salas",
            "SC_id_type__c": "CC",                        #
            "id_number__c": "1040011014",
            "sc_genero__c": "Femenino",
            "tipo_de_persona__c": "B2C",                  
            "sc_LGBTIQ__c": "No",
            "sc_Condicion_especial__c": "No aplica",
            
            # --- Datos de Contacto y Ubicación ---
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "camila@test.com",
            "direccion__c": "Calle 93 # 11-11",          
            "Departamento__c": "Bogotá D.C.",             
            "SC_municipio__c": "Bogotá D.C.",
            
            # --- Clasificación y Control del Caso ---
            "canal__c": "Internet",
            "punto_recepcion": "Manual",               
            "Instancia_de_recepcion__c": "Entidad vigilada", 
            "admision_col__c": "No Aplica",              
            "Status": "New",
            
            # --- Detalles de la Queja ---
            "Description": "Prueba de queja",
            "smart_anexo_queja__c": False,                 #
            "Tutela__c": "No",
            "Ente_de_control__c": "Otros",
            
            # --- Producto y Motivo (Tipificación) ---
            "Product__c": "Cuenta perfil",                
            "smart_Producto_nombre__c": "Ahorro",
            "Categorias_COL__c": "Transacción no reconocida"         
        }

    async def test_envio_exitoso_sin_anexos(self):
        """Prueba de despacho exitoso hacia la SFC cuando la queja no tiene archivos anexos."""
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"status": "created"})
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            s3_client=self.s3_client_mock
        )
        
        resultado = await service.ejecutar_envio_momento_2(self.mock_datos_consolidados)
        
        # Verificaciones
        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_nueva_queja.assert_called_once()

    async def test_envio_exitoso_con_anexos(self):
        """Prueba de descarga asíncrona de S3 y transmisión concurrente a la SFC."""
        datos_con_anexos = self.mock_datos_consolidados.copy()
        datos_con_anexos["smart_anexo_queja__c"] = True
        datos_con_anexos["archivos_s3"] = [
            {"s3_key": f"{self.smart_code}/soporte1.pdf", "bucket": "mi-bucket-smartsupervision"}
        ]
        
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"status": "created"})
        self.sfc_client_mock.post_adjunto_queja = AsyncMock(return_value={"status": "uploaded"})
        
        # Mocking S3 de AWS
        self.s3_client_mock.head_object = MagicMock(return_value={"ContentLength": 1024})
        mock_body = MagicMock()
        mock_body.read = MagicMock(return_value=b"bytes_pdf_simulados")
        self.s3_client_mock.get_object = MagicMock(return_value={"Body": mock_body})
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            s3_client=self.s3_client_mock
        )
        
        resultado = await service.ejecutar_envio_momento_2(datos_con_anexos)
        
        # Verificaciones
        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_nueva_queja.assert_called_once()
        self.sfc_client_mock.post_adjunto_queja.assert_called_once()

    async def test_envio_fallido_error_api_sfc(self):
        """Prueba el aislamiento de fallos: si la SFC falla con excepción genérica, capturamos el error."""
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=Exception("SFC Timeout Connection"))
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            s3_client=self.s3_client_mock
        )
        
        resultado = await service.ejecutar_envio_momento_2(self.mock_datos_consolidados)
        
        # Verificaciones
        self.assertEqual(resultado["status"], "error")
        self.assertIn("Pipeline interrumpido", resultado["message"])

    async def test_envio_fallido_sfc_integration_exception(self):
        """Prueba que si la SFC lanza una SfcIntegrationException, se propaga para que la atrape el router."""
        exc = SfcIntegrationException(
            status_code=400,
            error_type="DNI_INVALIDO",
            sfc_field="numero_id_CF",
            raw_message="El número de DNI es inválido",
            crm_action="Verificar el número de identificación del cliente"
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=exc)
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            s3_client=self.s3_client_mock
        )
        
        with self.assertRaises(SfcIntegrationException):
            await service.ejecutar_envio_momento_2(self.mock_datos_consolidados)

    async def test_missing_smart_code(self):
        """Valida que retorne un error si falta el campo obligatorio 'Smart_Code__c' en el payload."""
        payload_invalido = self.mock_datos_consolidados.copy()
        payload_invalido.pop("Smart_Code__c")
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            s3_client=self.s3_client_mock
        )
        
        resultado = await service.ejecutar_envio_momento_2(payload_invalido)
        self.assertEqual(resultado["status"], "error")
        self.assertIn("Falta el campo obligatorio 'Smart_Code__c'", resultado["message"])

if __name__ == "__main__":
    unittest.main()