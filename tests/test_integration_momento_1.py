import os
import unittest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, MagicMock, patch

from app.main import app
from app.api.dependencies import get_db, get_sfc_client
from app.models.quejas import Base, Queja
from app.integrations.sfc_client import SfcClient
from app.core.constants import SmartStatus

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_integration.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class TestCronIntegration(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.sfc_client_mock = MagicMock(spec=SfcClient)

        def override_get_db():
            db = TestingSessionLocal()
            try: yield db
            finally: db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        if os.path.exists("./test_integration.db"):
            os.remove("./test_integration.db")

    @patch("app.services.momento_1_sync.SincronizacionService._descargar_y_subir_a_s3", new_callable=AsyncMock)
    def test_cron_sync_multiple_quejas_mixed_attachments(self, mock_descarga_s3):
        """
        Prueba el flujo de sincronización validando nombres y valores 
        reales del diccionario de Salesforce.
        """
        mock_quejas_response = {
            "Response": {
                "count": "2",
                "results": [
                    {
                        "codigo_queja": "11111111111",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:00:00",
                        "nombres": "Camila Salas",
                        "anexo_queja": True,
                        "sexo": 1,                     # SFC: Femenino
                        "canal_cod": 13,               # SFC: Internet
                        "ente_control": 1,             # SFC: Procuraduría
                        "condicion_especial": 8,       # SFC: Mujer embarazada
                        "tipo_persona": 2              # SFC: Jurídica
                    },
                    {
                        "codigo_queja": "22222222222",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:05:00",
                        "nombres": "Juan Perez",
                        "anexo_queja": False,
                        "sexo": 2,                     # SFC: Masculino
                        "canal_cod": 14,               # SFC: Oficinas
                        "ente_control": 2,             # SFC: Contraloría
                        "condicion_especial": 1,       # SFC: Adulto mayor
                        "tipo_persona": 1              # SFC: Natural
                    }
                ]
            }
        }

        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(return_value={"Response": {"results": []}})
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value={"Response": {"pqrs_error": []}})

        response = self.client.post("/api/v1/quejas/sync/momento-1")
        self.assertEqual(response.status_code, 200)

        db_session = TestingSessionLocal()
        try:
            quejas = db_session.query(Queja).order_by(Queja.Smart_Code__c).all()
            self.assertEqual(len(quejas), 2)

            # --- VERIFICACIÓN DE MAPEO INTEGRADO REAL ---
            self.assertEqual(quejas[0].Smart_Code__c, "11111111111")
            self.assertEqual(quejas[0].smart_anexo_queja__c, True)
            self.assertEqual(quejas[0].sc_genero__c, "Femenino")
            self.assertEqual(quejas[0].canal__c, "Internet")
            self.assertEqual(quejas[0].Ente_de_control__c, "Procuraduría")
            self.assertEqual(quejas[0].sc_Condicion_especial__c, "Mujer embarazada")
            self.assertEqual(quejas[0].tipo_de_persona__c, "Jurídica")

            self.assertEqual(quejas[1].Smart_Code__c, "22222222222")
            self.assertEqual(quejas[1].smart_anexo_queja__c, False)
            self.assertEqual(quejas[1].sc_genero__c, "Masculino")
            self.assertEqual(quejas[1].canal__c, "Oficinas")
            self.assertEqual(quejas[1].sc_Condicion_especial__c, "Adulto mayor")
            self.assertEqual(quejas[1].tipo_de_persona__c, "Natural")
        finally:
            db_session.close()