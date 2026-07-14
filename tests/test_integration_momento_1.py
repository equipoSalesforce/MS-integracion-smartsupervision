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

# Base de datos SQLite local para pruebas concurrentes
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_integration.db"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class TestCronIntegration(unittest.TestCase):

    def setUp(self):
        # Creamos las tablas necesarias en nuestra base de datos de pruebas
        Base.metadata.create_all(bind=engine)
        self.sfc_client_mock = MagicMock(spec=SfcClient)

        # Definimos un generador de base de datos seguro para FastAPI
        def override_get_db():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        # Sobrescribimos dependencias en FastAPI
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
    def test_cron_sync_with_attachments_success(self, mock_descarga_s3):
        """
        Escenario 1: Sincronización exitosa con archivos anexos.
        La queja se crea, descarga sus adjuntos instantáneamente y transiciona hasta REPORT_ACK_OK.
        """
        mock_quejas_response = {
            "Response": {
                "count": "1",
                "results": [
                    {
                        "codigo_queja": "77777777777",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:00:00",
                        "nombres": "Cliente Con Anexos",
                        "numero_id_CF": "111222333",
                        "texto_queja": "Tengo problemas con mi cuenta",
                        "anexo_queja": True
                    }
                ]
            }
        }
        
        mock_adjuntos_response = {
            "Response": {
                "count": "1",
                "results": [
                    {
                        "id": "1001",
                        "file": "https://storage.googleapis.com/unscanned-sfc-smartsupervision-dev/77777777777/soporte.pdf?Expires=1637286826&Signature=abc",
                        "type": "pdf",
                        "state": 1,
                        "codigo_queja": "77777777777"
                    }
                ]
            }
        }

        mock_ack_response = {
            "Response": {
                "message": "Código actualizado",
                "pqrs_error": []
            }
        }

        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(return_value=mock_adjuntos_response)
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value=mock_ack_response)

        response = self.client.post("/api/v1/quejas/sync/momento-1")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["quejas"]["nuevas_quejas_descargadas"], 1)
        self.assertEqual(data["archivos"]["descargas_ok"], 1)
        self.assertEqual(data["ack"]["ack_exitosos"], 1)

        mock_descarga_s3.assert_called_once_with(
            "https://storage.googleapis.com/unscanned-sfc-smartsupervision-dev/77777777777/soporte.pdf?Expires=1637286826&Signature=abc",
            "77777777777/1001_pdf"
        )

        db_session = TestingSessionLocal()
        try:
            queja_en_db = db_session.query(Queja).filter(Queja.codigo_queja == "77777777777").first()
            self.assertIsNotNone(queja_en_db)
            self.assertEqual(queja_en_db.nombres, "Cliente Con Anexos")
            self.assertEqual(queja_en_db.status_smart, SmartStatus.REPORT_ACK_OK.value)
        finally:
            db_session.close()

    @patch("app.services.momento_1_sync.SincronizacionService._descargar_y_subir_a_s3", new_callable=AsyncMock)
    def test_cron_sync_with_attachments_download_error(self, mock_descarga_s3):
        """
        Escenario 2: Fallo al descargar adjuntos.
        La queja se crea en BD como 'Created', pero al fallar la descarga del adjunto temporal,
        debe marcarse como 'FileDownload-ERROR' y NO enviarse el ACK a la SFC.
        """
        mock_quejas_response = {
            "Response": {
                "count": "1",
                "results": [
                    {
                        "codigo_queja": "88888888888",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:00:00",
                        "nombres": "Cliente Descarga Fallida",
                        "numero_id_CF": "444555666",
                        "texto_queja": "Queja con adjuntos corruptos",
                        "anexo_queja": True
                    }
                ]
            }
        }

        mock_adjuntos_response = {
            "Response": {
                "count": "1",
                "results": [
                    {
                        "id": "2002",
                        "file": "https://storage.googleapis.com/unscanned-sfc-smartsupervision-dev/88888888888/soporte_roto.pdf",
                        "type": "pdf",
                        "state": 1,
                        "codigo_queja": "88888888888"
                    }
                ]
            }
        }

        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(return_value=mock_adjuntos_response)
        mock_descarga_s3.side_effect = Exception("HTTP Error 403: Signature expired")
        self.sfc_client_mock.send_ack_batch = AsyncMock()

        response = self.client.post("/api/v1/quejas/sync/momento-1")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(data["quejas"]["nuevas_quejas_descargadas"], 1)
        self.assertEqual(data["archivos"]["descargas_error"], 1)
        self.assertEqual(data["ack"]["ack_exitosos"], 0)

        db_session = TestingSessionLocal()
        try:
            queja_en_db = db_session.query(Queja).filter(Queja.codigo_queja == "88888888888").first()
            self.assertIsNotNone(queja_en_db)
            self.assertEqual(queja_en_db.status_smart, SmartStatus.FILE_DOWNLOAD_ERROR.value)
        finally:
            db_session.close()

        self.sfc_client_mock.send_ack_batch.assert_not_called()

    # ======================================================================
    # NUEVO ESCENARIO: MÚLTIPLES QUEJAS CON MIX DE ANEXOS (SÍ / NO)
    # ======================================================================
    @patch("app.services.momento_1_sync.SincronizacionService._descargar_y_subir_a_s3", new_callable=AsyncMock)
    def test_cron_sync_multiple_quejas_mixed_attachments(self, mock_descarga_s3):
        """
        Escenario 3: Obtiene múltiples quejas simultáneas de la SFC[cite: 1, 2].
        - Queja 11111111111: SÍ tiene anexos (debe descargar soporte y confirmarse)[cite: 1, 2].
        - Queja 22222222222: NO tiene anexos (debe saltar descarga e ir directo a confirmación)[cite: 1, 2].
        - Queja 33333333333: SÍ tiene anexos (debe descargar soporte y confirmarse)[cite: 1, 2].
        """
        # Mock 1: La SFC nos retorna tres quejas en una sola página[cite: 1, 2]
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
                        "anexo_queja": True  # -> Debe detonar descarga[cite: 1, 2]
                    },
                    {
                        "codigo_queja": "22222222222",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:05:00",
                        "nombres": "Queja Dos Sin Anexo",
                        "numero_id_CF": "1002",
                        "texto_queja": "Duda sobre tarifa de comisión",
                        "anexo_queja": False  # -> Debe ir directo a FILE_DOWNLOAD_OK sin llamar a storage[cite: 1, 2]
                    },
                    {
                        "codigo_queja": "33333333333",
                        "tipo_entidad": 1,
                        "entidad_cod": "423",
                        "fecha_creacion": "2026-07-14T12:10:00",
                        "nombres": "Queja Tres Con Anexo",
                        "numero_id_CF": "1003",
                        "texto_queja": "Bloqueo injustificado de tarjeta",
                        "anexo_queja": True  # -> Debe detonar descarga[cite: 1, 2]
                    }
                ]
            }
        }

        # Mock 2: Función dinámica para responder adjuntos diferentes según el ID consultado[cite: 2]
        async def dynamic_get_adjuntos(codigo_queja: str):
            if codigo_queja == "11111111111":
                return {
                    "Response": {
                        "count": "1",
                        "results": [
                            {
                                "id": "101",
                                "file": "https://storage.googleapis.com/unscanned-sfc-smartsupervision-dev/11111111111/soporte1.pdf?Expires=1637&Signature=aaa",
                                "type": "pdf",
                                "state": 1,
                                "codigo_queja": "11111111111"
                            }
                        ]
                    }
                }
            elif codigo_queja == "33333333333":
                return {
                    "Response": {
                        "count": "1",
                        "results": [
                            {
                                "id": "301",
                                "file": "https://storage.googleapis.com/unscanned-sfc-smartsupervision-dev/33333333333/soporte3.pdf?Expires=1637&Signature=ccc",
                                "type": "pdf",
                                "state": 1,
                                "codigo_queja": "33333333333"
                            }
                        ]
                    }
                }
            # Por seguridad, si llega a consultar la queja '22222222222' (que no tiene anexos), fallamos[cite: 1, 2]
            raise ValueError(f"No debió llamarse al listado de adjuntos para la queja {codigo_queja}[cite: 1, 2]")

        # Mock 3: Reporte de confirmación ACK para el lote completo[cite: 1, 2]
        mock_ack_response = {
            "Response": {
                "message": "Código actualizado",
                "pqrs_error": []  # Todo exitoso[cite: 1, 2]
            }
        }

        # Seteamos el comportamiento dinámico y las respuestas mockeadas[cite: 2]
        self.sfc_client_mock.fetch_quejas_pagina = AsyncMock(return_value=mock_quejas_response)
        self.sfc_client_mock.get_adjuntos_list = AsyncMock(side_effect=dynamic_get_adjuntos)
        self.sfc_client_mock.send_ack_batch = AsyncMock(return_value=mock_ack_response)

        # ----------------- EJECUCIÓN DEL CRON -----------------
        response = self.client.post("/api/v1/quejas/sync/momento-1")
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        
        # --- VERIFICACIÓN DE MÉTRICAS GLOBALES ---
        self.assertEqual(data["quejas"]["nuevas_quejas_descargadas"], 3)
        self.assertEqual(data["archivos"]["descargas_ok"], 3)  # Las 3 transicionaron exitosamente[cite: 1, 2]
        self.assertEqual(data["ack"]["ack_exitosos"], 3)       # El lote completo fue confirmado[cite: 1, 2]

        # --- VERIFICACIÓN DE DESCARGAS BINARIAS (AWS S3) ---
        # Verificamos que se hayan llamado solo las descargas de Google Storage correspondientes[cite: 2]
        self.assertEqual(mock_descarga_s3.call_count, 2)
        mock_descarga_s3.assert_has_calls([
            call(
                "https://storage.googleapis.com/unscanned-sfc-smartsupervision-dev/11111111111/soporte1.pdf?Expires=1637&Signature=aaa",
                "11111111111/101_pdf"
            ),
            call(
                "https://storage.googleapis.com/unscanned-sfc-smartsupervision-dev/33333333333/soporte3.pdf?Expires=1637&Signature=ccc",
                "33333333333/301_pdf"
            )
        ], any_order=True)

        # --- VERIFICACIÓN EN BASE DE DATOS (ESTADO FINAL) ---
        db_session = TestingSessionLocal()
        try:
            # Todas las quejas deben existir y estar en estado final reportACK-OK[cite: 1, 2]
            quejas = db_session.query(Queja).order_by(Queja.codigo_queja).all()
            self.assertEqual(len(quejas), 3)

            # Queja 1[cite: 1, 2]
            self.assertEqual(quejas[0].codigo_queja, "11111111111")
            self.assertEqual(quejas[0].status_smart, SmartStatus.REPORT_ACK_OK.value)
            
            # Queja 2[cite: 1, 2]
            self.assertEqual(quejas[1].codigo_queja, "22222222222")
            self.assertEqual(quejas[1].status_smart, SmartStatus.REPORT_ACK_OK.value)
            
            # Queja 3[cite: 1, 2]
            self.assertEqual(quejas[2].codigo_queja, "33333333333")
            self.assertEqual(quejas[2].status_smart, SmartStatus.REPORT_ACK_OK.value)
        finally:
            db_session.close()

        # Verificamos que el lote de ACK haya salido con las 3 quejas agrupadas[cite: 1, 2]
        self.sfc_client_mock.send_ack_batch.assert_called_once()
        sent_lote = self.sfc_client_mock.send_ack_batch.call_args[0][0]
        self.assertEqual(set(sent_lote), {"11111111111", "22222222222", "33333333333"})