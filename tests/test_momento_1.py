# tests/test_momento_1.py
import unittest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from app.main import app
from app.core.config import settings
from app.api.dependencies import get_sfc_client, get_s3_client
from app.integrations.sfc_client import SfcClient
from app.services.s3_service import S3StorageService
from app.services.momento_1_sync import SincronizacionService


class TestMomento1Pipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # 1. Mocks de clientes externos
        self.sfc_client_mock = MagicMock(spec=SfcClient)
        self.s3_client_mock = MagicMock()

        # 2. Dataset simulado de la SFC con los campos requeridos
        self.mock_quejas_response = {
            "Response": {
                "count": 1,
                "pages": 1,
                "next": None,
                "results": [
                    {
                        "codigo_queja": "142316551509974606",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-01-31T06:10:06",
                        "codigo_pais": "COL",
                        "departamento_cod": "11",
                        "municipio_cod": "11001",
                        "nombres": "Camila Salas",
                        "tipo_id_CF": 1,
                        "numero_id_CF": "1040011014",
                        "telefono": "3001234567",
                        "correo": "camila@test.com",
                        "tipo_persona": 1,
                        "sexo": 1,
                        "lgbtiq": False,
                        "canal_cod": 13,
                        "condicion_especial": 98,
                        "producto_cod": 209,
                        "producto_nombre": "Cuenta de Ahorros",
                        "macro_motivo_cod": 209,
                        "texto_queja": "Prueba de sincronización con ACK diferido",
                        "anexo_queja": True,  # Requiere descarga de S3
                        "tutela": False,
                        "ente_control": 99,
                        "escalamiento_DCF": False,
                        "replica": False,
                        "argumento_replica": None,
                        "desistimiento_queja": False,
                        "queja_expres": False
                    }
                ]
            }
        }
        
        # 3. Dataset simulado de listado de adjuntos
        self.mock_adjuntos_response = {
            "Response": {
                "count": 1,
                "results": [
                    {
                        "id": 13,
                        "file": "https://storage.googleapis.com/unscanned/test.pdf",
                        "type": "pdf",
                        "state": 1,
                        "codigo_queja": "142316551509974606"
                    }
                ]
            }
        }
        
        # 4. Respuesta simulada de confirmación ACK ante la SFC
        self.mock_ack_response = {
            "Response": {
                "message": "Código actualizado",
                "pqrs_error": []
            }
        }

    async def test_flujo_descarga_momento_1_exitoso_sin_ack_automatico(self):
        """
        Prueba que el flujo de descarga en Momento 1:
        1. Obtenga las quejas y procese adjuntos a S3.
        2. Aplique los mapeos de la SFC al esquema del CRM.
        3. NO invoque el ACK de manera automática (ACK diferido).
        """
        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=self.mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(return_value=self.mock_adjuntos_response)
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value=self.mock_ack_response)

        service = SincronizacionService(sfc_client=self.sfc_client_mock, s3_client=self.s3_client_mock)
        
        # 🎯 FIX: Mapeo correcto del método transferir_lote_sfc_a_s3 en S3StorageService
        service.s3_service.transferir_lote_sfc_a_s3 = AsyncMock(return_value=[
            {
                "codigo_queja": "142316551509974606",
                "nombre_archivo": "13.pdf",
                "s3_key": "quejas/142316551509974606/13_pdf",
                "bucket": "mi-bucket-smartsupervision"
            }
        ])

        resultado = await service.ejecutar_flujo_completo_momento_1()

        self.sfc_client_mock.fetch_quejas_pagina.assert_called_once()
        self.sfc_client_mock.get_adjuntos_list.assert_called_once_with("142316551509974606")
        self.sfc_client_mock.send_ack_batch.assert_not_called()

        self.assertIsInstance(resultado, list)
        self.assertEqual(len(resultado), 1)

        queja_mapeada = resultado[0]
        self.assertEqual(queja_mapeada["Smart_Code__c"], "142316551509974606")
        self.assertEqual(queja_mapeada["sc_genero__c"], "Femenino")
        self.assertEqual(queja_mapeada["canal__c"], "Internet")
        self.assertEqual(queja_mapeada["Ente_de_control__c"], "Otros")
        self.assertEqual(queja_mapeada["sc_Condicion_especial__c"], "No aplica")

        self.assertEqual(len(queja_mapeada["archivos_s3"]), 1)
        self.assertEqual(queja_mapeada["archivos_s3"][0]["nombre_archivo"], "13.pdf")

    async def test_confirmar_recepcion_ack_exitoso(self):
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value=self.mock_ack_response)
        service = SincronizacionService(sfc_client=self.sfc_client_mock)

        ids_a_confirmar = ["142316551509974606", "142316551509974607"]
        respuesta = await service.confirmar_recepcion_ack(ids_quejas=ids_a_confirmar)

        self.sfc_client_mock.send_ack_batch.assert_called_once_with(ids_a_confirmar)
        self.assertEqual(respuesta["status"], "success")
        self.assertEqual(respuesta["confirmados"], 2)
        self.assertEqual(respuesta["ids_procesados"], ids_a_confirmar)

    async def test_confirmar_recepcion_ack_lista_vacia(self):
        service = SincronizacionService(sfc_client=self.sfc_client_mock)

        respuesta = await service.confirmar_recepcion_ack(ids_quejas=[])

        self.sfc_client_mock.send_ack_batch.assert_not_called()
        self.assertEqual(respuesta["status"], "warning")
        self.assertEqual(respuesta["confirmados"], 0)

    async def test_flujo_descarga_sin_anexos(self):
        queja_sin_anexos = dict(self.mock_quejas_response["Response"]["results"][0])
        queja_sin_anexos["anexo_queja"] = False
        
        mock_response = {"Response": {"count": 1, "next": None, "results": [queja_sin_anexos]}}
        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=mock_response)

        service = SincronizacionService(sfc_client=self.sfc_client_mock)
        resultado = await service.ejecutar_flujo_completo_momento_1()

        self.sfc_client_mock.fetch_quejas_pagina.assert_called_once()
        self.sfc_client_mock.get_adjuntos_list.assert_not_called()
        self.assertEqual(len(resultado[0]["archivos_s3"]), 0)


class TestMomento1Integration(unittest.TestCase):

    def setUp(self):
        self.sfc_client_mock = MagicMock(spec=SfcClient)
        self.s3_client_mock = MagicMock()

        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock
        
        self.client = TestClient(app)
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

    def tearDown(self):
        app.dependency_overrides.clear()

    @patch.object(S3StorageService, "transferir_lote_sfc_a_s3", new_callable=AsyncMock)
    def test_cron_sync_multiple_quejas_mixed_attachments_sin_ack_automatico(self, mock_descarga_s3):
        mock_descarga_s3.return_value = [
            {
                "codigo_queja": "11111111111",
                "nombre_archivo": "soporte.pdf",
                "s3_key": "quejas/11111111111/soporte_pdf",
                "bucket": "mi-bucket-smartsupervision"
            }
        ]

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
                        "tipo_persona": 2,
                        "sexo": 1,
                        "lgbtiq": False,
                        "canal_cod": 13,
                        "condicion_especial": 8,
                        "producto_cod": 209,
                        "producto_nombre": "Ahorro",
                        "macro_motivo_cod": 209,
                        "texto_queja": "Texto largo 1",
                        "anexo_queja": True,
                        "tutela": False,
                        "ente_control": 1,
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
                        "tipo_persona": 1,
                        "sexo": 2,
                        "lgbtiq": False,
                        "canal_cod": 14,
                        "condicion_especial": 1,
                        "producto_cod": 110,
                        "producto_nombre": "TC",
                        "macro_motivo_cod": 110,
                        "texto_queja": "Texto largo 2",
                        "anexo_queja": False,
                        "tutela": False,
                        "ente_control": 2,
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

        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(return_value=mock_adjuntos_response)
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value={"Response": {"pqrs_error": []}})

        response = self.client.get("/api/v1/quejas/sync/momento-1")
        
        self.assertEqual(response.status_code, 200)
        self.sfc_client_mock.send_ack_batch.assert_not_called()
        
        quejas_mapeadas = response.json()
        self.assertEqual(len(quejas_mapeadas), 2)

        # Registro 1
        self.assertEqual(quejas_mapeadas[0]["Smart_Code__c"], "11111111111")
        self.assertEqual(quejas_mapeadas[0]["sc_genero__c"], "Femenino")
        self.assertEqual(quejas_mapeadas[0]["canal__c"], "Internet")
        self.assertEqual(quejas_mapeadas[0]["Ente_de_control__c"], "Procuraduría")
        self.assertEqual(quejas_mapeadas[0]["sc_Condicion_especial__c"], "Mujer embarazada")
        self.assertEqual(quejas_mapeadas[0]["tipo_de_persona__c"], "B2B")
        self.assertEqual(len(quejas_mapeadas[0]["archivos_s3"]), 1)

        # Registro 2
        self.assertEqual(quejas_mapeadas[1]["Smart_Code__c"], "22222222222")
        self.assertEqual(quejas_mapeadas[1]["sc_genero__c"], "Masculino")
        self.assertEqual(quejas_mapeadas[1]["canal__c"], "Oficinas")
        self.assertEqual(quejas_mapeadas[1]["Ente_de_control__c"], "Contraloría")
        self.assertEqual(quejas_mapeadas[1]["sc_Condicion_especial__c"], "Adulto mayor")
        self.assertEqual(quejas_mapeadas[1]["tipo_de_persona__c"], "B2C")
        self.assertEqual(len(quejas_mapeadas[0]["archivos_s3"]), 1)

    def test_confirmacion_ack_momento_1_exitoso(self):
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value={
            "Response": {
                "message": "Código actualizado",
                "pqrs_error": []
            }
        })

        ids_a_confirmar = ["11111111111", "22222222222"]
        payload = {"ids_quejas": ids_a_confirmar}

        response = self.client.post("/api/v1/quejas/sync/momento-1/ack", json=payload)

        self.assertEqual(response.status_code, 200)
        self.sfc_client_mock.send_ack_batch.assert_called_once_with(ids_a_confirmar)
        
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["confirmados"], 2)
        self.assertEqual(data["ids_procesados"], ids_a_confirmar)


if __name__ == "__main__":
    unittest.main()