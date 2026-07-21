import unittest
from unittest.mock import AsyncMock, MagicMock
from app.services.momento_1_sync import SincronizacionService
from app.integrations.sfc_client import SfcClient

class TestMomento1Pipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # 1. Mocks de clientes externos
        self.sfc_client_mock = MagicMock(spec=SfcClient)
        self.s3_client_mock = MagicMock()

        # 2. Dataset simulado de la SFC con los 29 campos requeridos
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
        # Configuración de mocks
        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=self.mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(return_value=self.mock_adjuntos_response)
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value=self.mock_ack_response)

        service = SincronizacionService(sfc_client=self.sfc_client_mock, s3_client=self.s3_client_mock)
        
        # Simulación de subida exitosa a S3
        service._descargar_y_subir_a_s3 = AsyncMock(return_value={
            "nombre_archivo": "13.pdf",
            "s3_key": "quejas/142316551509974606/13_pdf",
            "bucket": "mi-bucket-smartsupervision"
        })

        # Ejecución
        resultado = await service.ejecutar_flujo_completo_momento_1()

        # --- VERIFICACIONES ---
        # 1. Llamadas de red obligatorias
        self.sfc_client_mock.fetch_quejas_pagina.assert_called_once()
        self.sfc_client_mock.get_adjuntos_list.assert_called_once_with("142316551509974606")
        
        # 🚨 REGLA CRÍTICA: Garantizamos que NO se haya llamado al ACK automáticamente
        self.sfc_client_mock.send_ack_batch.assert_not_called()

        # 2. Estructura y mapeos retornados al CRM
        self.assertIsInstance(resultado, list)
        self.assertEqual(len(resultado), 1)

        queja_mapeada = resultado[0]
        self.assertEqual(queja_mapeada["Smart_Code__c"], "142316551509974606")
        self.assertEqual(queja_mapeada["sc_genero__c"], "Femenino")      # 1 -> Femenino
        self.assertEqual(queja_mapeada["canal__c"], "Internet")           # 13 -> Internet
        self.assertEqual(queja_mapeada["Ente_de_control__c"], "Otros")    # 99 -> Otros
        self.assertEqual(queja_mapeada["sc_Condicion_especial__c"], "No aplica")

        # 3. Adjuntos en S3 inyectados
        self.assertEqual(len(queja_mapeada["archivos_s3"]), 1)
        self.assertEqual(queja_mapeada["archivos_s3"][0]["nombre_archivo"], "13.pdf")

    async def test_confirmar_recepcion_ack_exitoso(self):
        """
        Prueba la transmisión manual del ACK en lote hacia la SFC.
        """
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value=self.mock_ack_response)
        service = SincronizacionService(sfc_client=self.sfc_client_mock)

        ids_a_confirmar = ["142316551509974606", "142316551509974607"]
        respuesta = await service.confirmar_recepcion_ack(ids_quejas=ids_a_confirmar)

        # Verificaciones
        self.sfc_client_mock.send_ack_batch.assert_called_once_with(ids_a_confirmar)
        self.assertEqual(respuesta["status"], "success")
        self.assertEqual(respuesta["confirmados"], 2)
        self.assertEqual(respuesta["ids_procesados"], ids_a_confirmar)

    async def test_confirmar_recepcion_ack_lista_vacia(self):
        """
        Prueba el comportamiento defensivo al enviar una lista vacía de IDs para ACK.
        """
        service = SincronizacionService(sfc_client=self.sfc_client_mock)

        respuesta = await service.confirmar_recepcion_ack(ids_quejas=[])

        # Verificaciones
        self.sfc_client_mock.send_ack_batch.assert_not_called()
        self.assertEqual(respuesta["status"], "warning")
        self.assertEqual(respuesta["confirmados"], 0)

    async def test_flujo_descarga_sin_anexos(self):
        """
        Prueba que si 'anexo_queja' es False, el servicio no intente consultar la lista de adjuntos.
        """
        # Copiamos la queja modificando anexo_queja a False
        queja_sin_anexos = dict(self.mock_quejas_response["Response"]["results"][0])
        queja_sin_anexos["anexo_queja"] = False
        
        mock_response = {"Response": {"count": 1, "next": None, "results": [queja_sin_anexos]}}
        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=mock_response)

        service = SincronizacionService(sfc_client=self.sfc_client_mock)
        resultado = await service.ejecutar_flujo_completo_momento_1()

        # Verificaciones
        self.sfc_client_mock.fetch_quejas_pagina.assert_called_once()
        self.sfc_client_mock.get_adjuntos_list.assert_not_called()
        self.assertEqual(len(resultado[0]["archivos_s3"]), 0)


if __name__ == "__main__":
    unittest.main()