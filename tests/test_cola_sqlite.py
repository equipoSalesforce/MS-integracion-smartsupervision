import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.future import select

from app.main import app
from app.core.config import settings
from app.db.database import Base
from app.models.cola_model import ColaDespachoModel
from app.workers.scheduler import reintentar_despachos_pendientes_job
from app.core.exceptions import SfcIntegrationException
from app.api.dependencies import get_sfc_client, get_s3_client


# 🛠️ Clases Mock dinámicas para interceptar cualquier llamada asíncrona a la SFC
class MockSfcClientSuccess:
    """Simula que cualquier método asíncrono invocado en la SFC responde exitosamente."""
    def __getattr__(self, name):
        return AsyncMock(return_value={"status": "success", "sfc_code": "SFC-999888777666"})

class MockSfcClientDown:
    """Simula que cualquier método asíncrono invocado en la SFC lanza error de servidor (502)."""
    def __getattr__(self, name):
        return AsyncMock(side_effect=SfcIntegrationException(
            status_code=502,
            error_type="SFC_DOWN",
            raw_message="Servidor de la SFC fuera de servicio (Bad Gateway)",
            sfc_field="general",
            crm_action="Reintentar automáticamente más tarde"
        ))

class MockSfcClientTimeout:
    """Simula un fallo de conexión o timeout contra la SFC."""
    def __getattr__(self, name):
        return AsyncMock(side_effect=Exception("SFC Connection Timeout"))


class TestColaSqliteContingencia(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # 1. Base de datos SQLite aislada en memoria para cada ejecución de test
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        self.async_session_factory = async_sessionmaker(
            bind=self.engine, class_=AsyncSession, expire_on_commit=False
        )

        # Crear esquema de tablas en la BD en memoria
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.s3_client_mock = MagicMock()
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock

        self.client = TestClient(app)
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

        # Payload CRM completo con los 29 campos requeridos por Pydantic
        self.payload_crm_test = {
            "Smart_Code__c": "999888777666",
            "CreatedDate": "2026-07-21T10:00:00",
            "Status": "New",
            "tipo_operacion": "AUTO",
            "SuppliedName": "Prueba Contingencia Cola",
            "SC_id_type__c": "CC",
            "id_number__c": "123456789",
            "sc_genero__c": "No Aplica",
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": "No",
            "sc_Condicion_especial__c": "No aplica",
            "direccion__c": "Calle 123",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "canal__c": "Internet",
            "punto_recepcion": "WhatsApp",
            "Instancia_de_recepcion__c": "Defensor del consumidor financiero",
            "Product__c": "Cuenta perfil",
            "Categorias_COL__c": "Transacción no reconocida",
            "Description": "Test de encolado automático por SFC abajo",
            "smart_anexo_queja__c": False,
            "smart_escalamiento_DCF__c": "No",
            "archivos_s3": []
        }

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        await self.engine.dispose()

    async def test_despacho_sfc_caida_encola_correctamente(self):
        """
        Escenario A: Cuando la SFC retorna error 502/SFC_DOWN,
        el endpoint responde 202 Accepted y guarda la queja en SQLite local.
        """
        app.dependency_overrides[get_sfc_client] = lambda: MockSfcClientDown()

        with patch("app.api.routes_quejas.AsyncSessionLocal", self.async_session_factory):
            response = self.client.post("/api/v1/quejas/sync/despacho", json=self.payload_crm_test)

            # Verificaciones de respuesta HTTP
            self.assertEqual(response.status_code, 202)
            data = response.json()
            self.assertEqual(data["status"], "queued")
            self.assertEqual(data["smart_code"], "999888777666")

            # Verificación de persistencia en SQLite
            async with self.async_session_factory() as session:
                stmt = select(ColaDespachoModel).where(ColaDespachoModel.smart_code == "999888777666")
                result = await session.execute(stmt)
                registro = result.scalars().first()

                self.assertIsNotNone(registro)
                self.assertEqual(registro.estado, "PENDIENTE")
                self.assertEqual(registro.intentos, 1)

    async def test_job_scheduler_reintenta_y_marca_exitoso(self):
        """
        Escenario B: El worker procesa los pendientes y, si la SFC responde OK,
        marca el registro en SQLite como EXITOSO.
        """
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        sfc_success_mock = MockSfcClientSuccess()

        # 1. Registrar caso PENDIENTE en SQLite
        async with self.async_session_factory() as session:
            item = ColaDespachoModel(
                smart_code="999888777666",
                tipo_operacion="AUTO",
                payload_json=self.payload_crm_test,
                estado="PENDIENTE",
                intentos=1,
                proximo_reintento_at=now_utc - timedelta(minutes=1)
            )
            session.add(item)
            await session.commit()

        # 2. Ejecutar el Job con la SFC simulada exitosa
        with patch("app.workers.scheduler.AsyncSessionLocal", self.async_session_factory), \
             patch("app.workers.scheduler.get_sfc_client", return_value=sfc_success_mock), \
             patch("app.workers.scheduler.get_s3_client", return_value=self.s3_client_mock):

            await reintentar_despachos_pendientes_job()

        # 3. Validar estado en SQLite
        async with self.async_session_factory() as session:
            stmt = select(ColaDespachoModel).where(ColaDespachoModel.smart_code == "999888777666")
            result = await session.execute(stmt)
            registro = result.scalars().first()

            self.assertEqual(registro.estado, "EXITOSO")

    async def test_job_scheduler_falla_incrementa_intentos(self):
        """
        Escenario C: Si la SFC sigue caída en el reintento,
        mantiene el estado PENDIENTE e incrementa la cantidad de intentos.
        """
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        sfc_timeout_mock = MockSfcClientTimeout()

        async with self.async_session_factory() as session:
            item = ColaDespachoModel(
                smart_code="999888777666",
                tipo_operacion="AUTO",
                payload_json=self.payload_crm_test,
                estado="PENDIENTE",
                intentos=1,
                proximo_reintento_at=now_utc - timedelta(minutes=1)
            )
            session.add(item)
            await session.commit()

        with patch("app.workers.scheduler.AsyncSessionLocal", self.async_session_factory), \
             patch("app.workers.scheduler.get_sfc_client", return_value=sfc_timeout_mock), \
             patch("app.workers.scheduler.get_s3_client", return_value=self.s3_client_mock):

            await reintentar_despachos_pendientes_job()

        async with self.async_session_factory() as session:
            stmt = select(ColaDespachoModel).where(ColaDespachoModel.smart_code == "999888777666")
            result = await session.execute(stmt)
            registro = result.scalars().first()

            self.assertEqual(registro.estado, "PENDIENTE")
            self.assertEqual(registro.intentos, 2)


if __name__ == "__main__":
    unittest.main()