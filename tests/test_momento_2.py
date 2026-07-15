import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.orm import Session

from app.services.momento_2_sync import Momento2SincronizacionService
from app.integrations.sfc_client import SfcClient

class TestMomento2Pipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # 1. Mocks de dependencias
        self.db_mock = MagicMock(spec=Session)
        self.sfc_client_mock = MagicMock(spec=SfcClient)
        self.s3_client_mock = MagicMock()
        
        self.smart_code = "142316551509974606"
        
        # 2. Diccionario simulado de base de datos relacional (Caso + Cuenta)
        self.mock_datos_consolidados = {
            "Smart_Code__c": self.smart_code,
            "CreatedDate": "2026-07-14T12:00:00",
            "SuppliedName": "Camila Salas",
            "LastName": "Salas",
            "id_type__c": 1,
            "id_number__c": "1040011014",
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "camila@test.com",
            "canal__c": "Internet",
            "Product__c": 209,
            "smart_Producto_nombre__c": "Producto A",
            "Categorias_COL__c": 209,
            "Description": "Prueba de queja",
            "smart_anexo_queja__c": False,
            "Urgent_Case__c": False,
            "Ente_de_control__c": "Otros",
            "Instancia_de_recepcion__c": 1,
            "sc_genero__c": "Femenino",
            "sc_Condicion_especial__c": "No aplica",
            "tipo_de_persona__c": "Natural",
            "tipo_entidad": 1,
            "entidad_cod": "423",
            "SC_municipio__c": "Bogotá D.C.",
            "Departamento__c": "Bogotá",
            "admision_col__c": 1
        }

    @patch("app.models.quejas_crud.QuejasCRUD.obtener_datos_consolidados_caso")
    @patch("app.models.quejas_crud.QuejasCRUD.actualizar_estado_caso")
    async def test_envio_exitoso_sin_anexos(self, mock_update, mock_get_caso):
        """Prueba de despacho exitoso hacia la SFC cuando la queja no tiene archivos anexos."""
        mock_get_caso.return_value = self.mock_datos_consolidados
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"status": "created"})
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            db=self.db_mock, 
            s3_client=self.s3_client_mock
        )
        
        resultado = await service.ejecutar_envio_momento_2(self.smart_code)
        
        # Verificaciones
        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_nueva_queja.assert_called_once()
        mock_update.assert_called_once_with(
            db=self.db_mock,
            smart_code=self.smart_code,
            status_smart="sendToSmart-OK",
            sfc_status="sendToSmart-OK"
        )

    @patch("app.models.quejas_crud.QuejasCRUD.obtener_datos_consolidados_caso")
    @patch("app.models.quejas_crud.QuejasCRUD.actualizar_estado_caso")
    async def test_envio_exitoso_con_anexos(self, mock_update, mock_get_caso):
        """Prueba de descarga asíncrona de S3 y transmisión concurrente a la SFC."""
        datos_con_anexos = self.mock_datos_consolidados.copy()
        datos_con_anexos["smart_anexo_queja__c"] = True
        mock_get_caso.return_value = datos_con_anexos
        
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"status": "created"})
        self.sfc_client_mock.post_adjunto_queja = AsyncMock(return_value={"status": "uploaded"})
        
        # Mocking S3 de AWS
        self.s3_client_mock.list_objects_v2 = MagicMock(return_value={
            "Contents": [
                {"Key": f"{self.smart_code}/soporte1.pdf", "Size": 1024}
            ]
        })
        mock_body = MagicMock()
        mock_body.read = MagicMock(return_value=b"bytes_pdf_simulados")
        self.s3_client_mock.get_object = MagicMock(return_value={"Body": mock_body})
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            db=self.db_mock, 
            s3_client=self.s3_client_mock
        )
        
        resultado = await service.ejecutar_envio_momento_2(self.smart_code)
        
        # Verificaciones
        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_nueva_queja.assert_called_once()
        self.sfc_client_mock.post_adjunto_queja.assert_called_once()
        mock_update.assert_called_once_with(
            db=self.db_mock,
            smart_code=self.smart_code,
            status_smart="sendToSmart-OK",
            sfc_status="sendToSmart-OK"
        )

    @patch("app.models.quejas_crud.QuejasCRUD.obtener_datos_consolidados_caso")
    @patch("app.models.quejas_crud.QuejasCRUD.actualizar_estado_caso")
    async def test_envio_fallido_error_api_sfc(self, mock_update, mock_get_caso):
        """Prueba el aislamiento de fallos: si la SFC falla, persistimos el error en base de datos."""
        mock_get_caso.return_value = self.mock_datos_consolidados
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=Exception("SFC Timeout Connection"))
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            db=self.db_mock
        )
        
        resultado = await service.ejecutar_envio_momento_2(self.smart_code)
        
        # Verificaciones
        self.assertEqual(resultado["status"], "error")
        mock_update.assert_called_once_with(
            db=self.db_mock,
            smart_code=self.smart_code,
            status_smart="sendToSmart-Error",
            sfc_status="sendToSmart-Error"
        )

    @patch("app.models.quejas_crud.QuejasCRUD.obtener_datos_consolidados_caso")
    async def test_caso_no_encontrado_en_db(self, mock_get_caso):
        """Valida el control de errores si el CRM invoca un Smart Code inexistente."""
        mock_get_caso.return_value = None
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            db=self.db_mock
        )
        
        resultado = await service.ejecutar_envio_momento_2(self.smart_code)
        self.assertEqual(resultado["status"], "error")
        self.assertIn("no encontrado", resultado["message"])

if __name__ == "__main__":
    unittest.main()