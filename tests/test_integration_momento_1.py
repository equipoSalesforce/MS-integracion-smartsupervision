import os
import unittest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, MagicMock, patch, call

from app.main import app
from app.api.dependencies import get_db, get_sfc_client
from app.models.quejas import Base, Queja
from app.integrations.sfc_client import SfcClient
from app.core.constants import SmartStatus

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_integration.db"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class TestCronIntegration(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.sfc_client_mock = MagicMock(spec=SfcClient)

        def override_get_db():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        if os.path.exists("./test_integration.db"):
            try:
                os.remove("./test_integration.db")
            except Exception:
                pass

    @patch("app.services.momento_1_sync.SincronizacionService._descargar_y_subir_a_s3", new_callable=AsyncMock)
    def test_cron_sync_multiple_quejas_mixed_attachments(self, mock_descarga_s3):
        """
        Prueba la sincronización con base de datos mapeada a Salesforce.
        Se descargan tres quejas mixtas.
        """
        mock_quejas_response = {
            "Response": {
                "count": "3",
                "results": [
                    {
                        "codigo_queja": "11111111111",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:00:00",
                        "nombres": "Queja Uno Con Anexo",
                        "numero_id_CF": "1001",
                        "texto_queja": "Error en transferencia cobrada doble",
                        "anexo_queja": True
                    },
                    {
                        "codigo_queja": "22222222222",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:05:00",
                        "nombres": "Queja Dos Sin Anexo",
                        "numero_id_CF": "1002",
                        "texto_queja": "Duda sobre tarifa de comisión",
                        "anexo_queja": False
                    },
                    {
                        "codigo_queja": "33333333333",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:10:00",
                        "nombres": "Queja Tres Con Anexo",
                        "numero_id_CF": "1003",
                        "texto_queja": "Bloqueo injustificado de tarjeta",
                        "anexo_queja": True
                    }
                ]
            }
        }

        async def dynamic_get_adjuntos(codigo_queja: str):
            if codigo_queja == "11111111111":
                return {
                    "Response": {
                        "results": [
                            {
                                "id": "101",
                                "file": "https://storage.googleapis.com/unscanned-sfc-smartsupervision-dev/11111111111/soporte1.pdf",
                                "type": "pdf",
                                "codigo_queja": "11111111111"
                            }
                        ]
                    }
                }
            elif codigo_queja == "33333333333":
                return {
                    "Response": {
                        "results": [
                            {
                                "id": "301",
                                "file": "https://storage.googleapis.com/unscanned-sfc-smartsupervision-dev/33333333333/soporte3.pdf",
                                "type": "pdf",
                                "codigo_queja": "33333333333"
                            }
                        ]
                    }
                }
            raise ValueError(f"Llamada inválida para {codigo_queja}")

        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(side_effect=dynamic_get_adjuntos)
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value={"Response": {"pqrs_error": []}})

        response = self.client.post("/api/v1/quejas/sync/momento-1")
        self.assertEqual(response.status_code, 200)

        # Validamos aserciones en Base de Datos usando la estructura de Salesforce
        db_session = TestingSessionLocal()
        try:
            quejas = db_session.query(Queja).order_by(Queja.Smart_Code__c).all()
            self.assertEqual(len(quejas), 3)

            # Validamos traducción de columnas
            self.assertEqual(quejas[0].Smart_Code__c, "11111111111")
            self.assertEqual(quejas[0].SuppliedName, "Queja Uno Con Anexo")
            self.assertEqual(quejas[0].archivo_adjunto__c, True)
            self.assertEqual(quejas[0].status_smart, SmartStatus.REPORT_ACK_OK.value)

            self.assertEqual(quejas[1].Smart_Code__c, "22222222222")
            self.assertEqual(quejas[1].SuppliedName, "Queja Dos Sin Anexo")
            self.assertEqual(quejas[1].archivo_adjunto__c, False)
        finally:
            db_session.close()