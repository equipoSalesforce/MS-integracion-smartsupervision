# tests/test_momento_1.py
import unittest
from unittest.mock import AsyncMock, MagicMock
from app.services.momento_1_sync import SincronizacionService
from app.integrations.sfc_client import SfcClient

class TestMomento1Pipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # 1. Mock del cliente de la SFC y S3
        self.sfc_client_mock = MagicMock(spec=SfcClient)
        self.s3_client_mock = MagicMock()  # Mock simple para S3

        # 2. Datos simulados completos de la SFC (Momento 1) con los 29 campos obligatorios
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
                        "texto_queja": "Prueba de sincronización",
                        "anexo_queja": True,  # Tiene adjuntos
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
        
        # 3. Datos simulados de listado de adjuntos
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
        
        # 4. Respuesta simulada de confirmación ACK
        self.mock_ack_response = {
            "Response": {
                "message": "Código actualizado",
                "pqrs_error": []  # Sin errores
            }
        }

    async def test_flujo_completo_momento_1_exitoso(self):
        """
        Prueba el flujo completo del Momento 1 en memoria (Stateless):
        1. Consulta quejas nuevas.
        2. Simula descargas S3.
        3. Envía el ACK.
        4. Retorna el payload mapeado en español con anexos.
        """
        # Configuramos los retornos del cliente SFC
        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=self.mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(return_value=self.mock_adjuntos_response)
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value=self.mock_ack_response)

        # Instanciamos el servicio síncrono en memoria
        service = SincronizacionService(sfc_client=self.sfc_client_mock, s3_client=self.s3_client_mock)
        
        # Simulamos que la subida/descarga a S3 es exitosa y retorna la metadata del archivo
        service._descargar_y_subir_a_s3 = AsyncMock(return_value={
            "nombre_archivo": "13.pdf",
            "s3_key": "quejas/142316551509974606/13_pdf",
            "bucket": "mi-bucket-smartsupervision"
        })

        # Ejecutamos el pipeline completo
        resultado = await service.ejecutar_flujo_completo_momento_1()

        # --- VERIFICACIONES ---
        # 1. Validamos llamadas de red a la SFC
        self.sfc_client_mock.fetch_quejas_pagina.assert_called_once()
        self.sfc_client_mock.get_adjuntos_list.assert_called_once_with("142316551509974606")
        self.sfc_client_mock.send_ack_batch.assert_called_once_with(["142316551509974606"])

        # 2. Validamos que el retorno sea una lista y contenga exactamente un registro mapeado
        self.assertIsInstance(resultado, list)
        self.assertEqual(len(resultado), 1)

        # 3. Validamos que las traducciones de Picklists (códigos -> texto) del CRM se hayan aplicado
        queja_mapeada = resultado[0]
        self.assertEqual(queja_mapeada["Smart_Code__c"], "142316551509974606")
        self.assertEqual(queja_mapeada["sc_genero__c"], "Femenino")  # 1 -> Femenino
        self.assertEqual(queja_mapeada["canal__c"], "Internet")       # 13 -> Internet
        self.assertEqual(queja_mapeada["Ente_de_control__c"], "Otros") # 99 -> Otros
        self.assertEqual(queja_mapeada["sc_Condicion_especial__c"], "No aplica") # 98 -> No aplica

        # 4. Validamos que los metadatos del archivo de S3 estén inyectados en la respuesta
        self.assertEqual(len(queja_mapeada["archivos_s3"]), 1)
        self.assertEqual(queja_mapeada["archivos_s3"][0]["nombre_archivo"], "13.pdf")

if __name__ == "__main__":
    unittest.main()