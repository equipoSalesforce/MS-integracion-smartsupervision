import unittest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy.orm import Session
from app.services.momento_1_sync import SincronizacionService
from app.models.quejas import Queja
from app.integrations.sfc_client import SfcClient
from app.core.constants import SmartStatus

class TestMomento1Pipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # 1. Mock de la base de datos (Session de SQLAlchemy)
        self.db_mock = MagicMock(spec=Session)
        
        # 2. Mock del cliente de la SFC
        self.sfc_client_mock = MagicMock(spec=SfcClient)
        
        # 3. Datos simulados de la API de la SFC (Momento 1) con envoltura "Response"
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
                        "fecha_creacion": "2024-01-31T06:10:06",
                        "nombres": "Camila Salas",
                        "numero_id_CF": "1040011014",
                        "texto_queja": "Prueba de sincronización",
                        "anexo_queja": True,  # Tiene adjuntos para forzar descarga
                        "macro_motivo_cod": 209,
                        "producto_cod": 209,
                        "canal_cod": 13
                    }
                ]
            }
        }
        
        # 4. Datos simulados de listado de adjuntos con envoltura "Response"
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
        
        # 5. Respuesta simulada del ACK
        self.mock_ack_response = {
            "Response": {
                "message": "Código actualizado",
                "pqrs_error": []  # Sin errores para que marque reportACK-OK
            }
        }

    async def test_flujo_completo_momento_1_exitoso(self):
        """
        Prueba el flujo completo secuencial del Momento 1:
        Creación (Created) -> Descarga (FileDownload-OK) -> Confirmación (reportACK-OK).
        """
        # Configuramos los retornos asíncronos de los mocks del cliente SFC
        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=self.mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(return_value=self.mock_adjuntos_response)
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value=self.mock_ack_response)

        # Mock de consulta a la Base de Datos utilizando nomenclatura de Salesforce (Smart_Code__c)
        # Primera consulta: Verificar si la queja existe al descargar (retorna None = Nueva)
        # Segunda consulta: Obtener quejas para descargar adjuntos (retorna nuestra queja mock)
        # Tercera consulta: Obtener quejas para el lote ACK (retorna nuestra queja mock)
        queja_db_mock = Queja(
            Smart_Code__c="142316551509974606",
            status_smart=SmartStatus.CREATED.value,
            smart_anexo_queja__c=True
        )
        
        # Configuramos el mock de la consulta para que devuelva None la primera vez, y luego la queja
        self.db_mock.query().filter().first.side_effect = [None, queja_db_mock]
        self.db_mock.query().filter().all.side_effect = [[queja_db_mock], [queja_db_mock]]

        # Instanciamos el servicio de sincronización
        service = SincronizacionService(sfc_client=self.sfc_client_mock, db=self.db_mock)
        service._descargar_y_subir_a_s3 = AsyncMock()

        # Ejecutamos el pipeline completo del Momento 1
        resultado = await service.ejecutar_flujo_completo_momento_1()

        # --- VERIFICACIONES ---
        
        # 1. Comprobamos que el servicio haya consultado y llamado a los endpoints correctos usando el ID canónico
        self.sfc_client_mock.fetch_quejas_pagina.assert_called_once()
        self.sfc_client_mock.get_adjuntos_list.assert_called_once_with("142316551509974606")
        self.sfc_client_mock.send_ack_batch.assert_called_once_with(["142316551509974606"])

        # 2. Verificamos que se haya ejecutado el guardado y commit en base de datos
        self.assertEqual(self.db_mock.add.call_count, 1)  # Se agregó la queja nueva mapeada
        self.assertTrue(self.db_mock.commit.called)

        # 3. Verificamos la transición final de estados de la máquina de estados
        # Como todo fue exitoso, el estado final en el objeto queja de la BD debe ser "reportACK-OK"
        self.assertEqual(queja_db_mock.status_smart, SmartStatus.REPORT_ACK_OK.value)

        # 4. Verificamos la estructura de la respuesta final del orquestador
        self.assertEqual(resultado["status"], "success")
        self.assertEqual(resultado["quejas"]["nuevas_quejas_descargadas"], 1)
        self.assertEqual(resultado["archivos"]["descargas_ok"], 1)
        self.assertEqual(resultado["ack"]["ack_exitosos"], 1)

if __name__ == "__main__":
    unittest.main()