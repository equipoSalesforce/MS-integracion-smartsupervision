import unittest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from app.main import app
from app.core.config import settings  # 👈 Importamos la configuración para obtener la API Key
from app.api.dependencies import get_sfc_client, get_s3_client
from app.integrations.sfc_client import SfcClient

class TestMomento1Integration(unittest.TestCase):

    def setUp(self):
        # Mock del cliente SFC y S3
        self.sfc_client_mock = MagicMock(spec=SfcClient)
        self.s3_client_mock = MagicMock()

        # Sobrescribimos las dependencias en FastAPI
        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock
        
        self.client = TestClient(app)
        
        # 🎯 INYECCIÓN DE API KEY: Permite al cliente HTTP de pruebas pasar la barrera de seguridad
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

    def tearDown(self):
        app.dependency_overrides.clear()

    @patch("app.services.momento_1_sync.SincronizacionService._descargar_y_subir_a_s3", new_callable=AsyncMock)
    def test_cron_sync_multiple_quejas_mixed_attachments_sin_ack_automatico(self, mock_descarga_s3):
        """
        Prueba el endpoint de integración de sincronización de punta a punta.
        Verifica que se consuma la SFC, se suban los archivos a S3 y FastAPI devuelva 
        las quejas perfectamente traducidas al CRM en el HTTP Response Body, 
        Garantizando que NO se envíe el ACK de manera automática (ACK diferido).
        """
        # Configuramos que S3 simule retornar datos válidos cuando se ejecute la descarga
        mock_descarga_s3.return_value = {
            "nombre_archivo": "soporte.pdf",
            "s3_key": "quejas/11111111111/soporte_pdf",
            "bucket": "mi-bucket-smartsupervision"
        }

        # JSON de respuesta con estructura nativa de la SFC (2 registros)
        mock_quejas_response = {
            "Response": {
                "count": 2,
                "results": [
                    {
                        "codigo_queja": "11111111111",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:00:00",
                        "codigo_pais": "COL",
                        "departamento_cod": "11",
                        "municipio_cod": "11001",
                        "nombres": "Camila Salas",
                        "tipo_id_CF": 1,
                        "numero_id_CF": "1040011014",
                        "telefono": "3000000000",
                        "correo": "camila@test.com",
                        "tipo_persona": 2, # Jurídica
                        "sexo": 1,         # Femenino
                        "lgbtiq": False,
                        "canal_cod": 13,   # Internet
                        "condicion_especial": 8, # Mujer embarazada
                        "producto_cod": 209,
                        "producto_nombre": "Ahorro",
                        "macro_motivo_cod": 209,
                        "texto_queja": "Texto largo 1",
                        "anexo_queja": True, # Forzar anexos
                        "tutela": False,
                        "ente_control": 1, # Procuraduría
                        "escalamiento_DCF": False,
                        "replica": False,
                        "argumento_replica": None,
                        "desistimiento_queja": False,
                        "queja_expres": False,
                        "direccion": "direccion 123"
                    },
                    {
                        "codigo_queja": "22222222222",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:05:00",
                        "codigo_pais": "COL",
                        "departamento_cod": "05",
                        "municipio_cod": "05001",
                        "nombres": "Juan Perez",
                        "tipo_id_CF": 1,
                        "numero_id_CF": "203002202",
                        "telefono": "3111111111",
                        "correo": "juan@test.com",
                        "tipo_persona": 1, # Natural
                        "sexo": 2,         # Masculino
                        "lgbtiq": False,
                        "canal_cod": 14,   # Oficinas
                        "condicion_especial": 1, # Adulto mayor
                        "producto_cod": 110,
                        "producto_nombre": "TC",
                        "macro_motivo_cod": 110,
                        "texto_queja": "Texto largo 2",
                        "anexo_queja": False, # Sin anexos
                        "tutela": False,
                        "ente_control": 2, # Contraloría
                        "escalamiento_DCF": False,
                        "replica": False,
                        "argumento_replica": None,
                        "desistimiento_queja": False,
                        "queja_expres": False,
                        "direccion": "direccion 123"
                    }
                ]
            }
        }

        # Simulación de respuesta de adjuntos para que no llegue vacía
        mock_adjuntos_response = {
            "Response": {
                "count": 1,
                "results": [
                    {
                        "id": 999,
                        "file": "https://storage.googleapis.com/unscanned/soporte.pdf",
                        "type": "pdf",
                        "state": 1,
                        "codigo_queja": "11111111111"
                    }
                ]
            }
        }

        # Configuración de los retornos asíncronos en los mocks
        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(return_value=mock_adjuntos_response)
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value={"Response": {"pqrs_error": []}})

        # Hacemos la petición POST al endpoint de sincronización
        response = self.client.post("/api/v1/quejas/sync/momento-1")
        
        # --- VERIFICACIONES SÍNCRONAS ---
        self.assertEqual(response.status_code, 200)
        
        # 🚨 REGLA CRÍTICA: Validamos que NO se haya llamado al ACK de la SFC durante el consumo
        self.sfc_client_mock.send_ack_batch.assert_not_called()
        
        # Obtenemos la lista directamente del Response Body
        quejas_mapeadas = response.json()
        self.assertEqual(len(quejas_mapeadas), 2)

        # Registro 1 (Traducido)
        self.assertEqual(quejas_mapeadas[0]["Smart_Code__c"], "11111111111")
        self.assertEqual(quejas_mapeadas[0]["sc_genero__c"], "Femenino")
        self.assertEqual(quejas_mapeadas[0]["canal__c"], "Internet")
        self.assertEqual(quejas_mapeadas[0]["Ente_de_control__c"], "Procuraduría")
        self.assertEqual(quejas_mapeadas[0]["sc_Condicion_especial__c"], "Mujer embarazada")
        self.assertEqual(quejas_mapeadas[0]["tipo_de_persona__c"], "B2B")
        self.assertEqual(len(quejas_mapeadas[0]["archivos_s3"]), 1)

        # Registro 2 (Traducido)
        self.assertEqual(quejas_mapeadas[1]["Smart_Code__c"], "22222222222")
        self.assertEqual(quejas_mapeadas[1]["sc_genero__c"], "Masculino")
        self.assertEqual(quejas_mapeadas[1]["canal__c"], "Oficinas")
        self.assertEqual(quejas_mapeadas[1]["Ente_de_control__c"], "Contraloría")
        self.assertEqual(quejas_mapeadas[1]["sc_Condicion_especial__c"], "Adulto mayor")
        self.assertEqual(quejas_mapeadas[1]["tipo_de_persona__c"], "B2C")
        self.assertEqual(len(quejas_mapeadas[1]["archivos_s3"]), 0)

    def test_confirmacion_ack_momento_1_exitoso(self):
        """
        Prueba de integración del nuevo endpoint de ACK:
        POST /api/v1/quejas/sync/momento-1/ack
        Verifica que reciba el arreglo de IDs confirmados por el CRM y comunique el ACK a la SFC.
        """
        # Mapeamos la respuesta del mock de la SFC
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value={
            "Response": {
                "message": "Código actualizado",
                "pqrs_error": []
            }
        })

        ids_a_confirmar = ["11111111111", "22222222222"]
        payload = {"ids_quejas": ids_a_confirmar}

        # Consumimos el endpoint
        response = self.client.post("/api/v1/quejas/sync/momento-1/ack", json=payload)

        # Verificaciones HTTP y Payload
        self.assertEqual(response.status_code, 200)
        
        # Verificamos que el cliente HTTP invocara el cliente SFC con los IDs esperados
        self.sfc_client_mock.send_ack_batch.assert_called_once_with(ids_a_confirmar)
        
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["confirmados"], 2)
        self.assertEqual(data["ids_procesados"], ids_a_confirmar)


if __name__ == "__main__":
    unittest.main()