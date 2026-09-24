# tests/test_integration_momento_3_real_redis.py
"""
Cobertura de integración de punta a punta para Momento 3 (trámite/self-healing)
vía el endpoint HTTP real, con Redis REAL detrás de idempotencia y del lock por
caso -- hasta ahora, ningún test combinaba estas tres cosas a la vez:

  - `test_momento_3.py` (TestMomento3UnitAndIntegration) usa TestClient real,
    pero Redis está MOCKEADO con un AsyncMock genérico (`.get.return_value = None`,
    `.set.return_value = True`) -- nunca ejercita los scripts Lua reales de
    idempotencia ni el RedisLock real.
  - `test_routes_quejas_despacho_lock_real_redis.py` sí usa Redis real para el
    lock/idempotencia/cola, pero mockea el `DespachoQuejaOrquestador` COMPLETO --
    nunca ejercita el self-healing M2→M3 real, ni la interacción real entre
    `IdempotencyService.mantener_processing_vivo` (Redis real) y el pipeline
    real de Momento 3.
  - No existe ningún `test_integration_momento_3.py` -- a diferencia de Momento
    1, 2 y 4, que sí tienen su propio archivo de integración dedicado.

Este archivo cierra esa combinación: HTTP real -> orquestador real -> Momento2/3
reales -> mapeo real (catálogo local) -> sólo se mockean los clientes SFC/S3 en
el límite de inyección de dependencias (mismo nivel que ya usa test_momento_3.py),
con Redis real detrás de TODO lo demás.

Se usa `httpx.AsyncClient` + `ASGITransport` dentro de `IsolatedAsyncioTestCase`
(no el `TestClient` síncrono) para que la app, el cliente HTTP y el cliente
Redis compartan el MISMO event loop de principio a fin -- mismo criterio que
`test_routes_quejas_despacho_lock_real_redis.py`: `redis.asyncio` ata sus
conexiones al loop en el que se crean, y mezclar `TestClient` (que corre su
propio loop interno) con un cliente Redis creado en otro loop produce fallos
de "Event loop is closed" / 503 IDEMPOTENCY_STORE_UNAVAILABLE en cuanto la
app intenta usarlo desde su loop.
"""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

import httpx

from app.main import app
from app.core.config import settings
from app.api.dependencies import get_sfc_client, get_s3_client
from app.core.exceptions import SfcIntegrationException
from app.services.queue_service import DESPACHO_LOCK_PREFIX
from app.services.idempotency_service import IdempotencyService
from app.schemas.crm_payloads import QuejaUnificadaCrmInput

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


def _redis_disponible() -> bool:
    if redis_asyncio is None:
        return False

    async def _check():
        client = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        try:
            await client.ping()
            return True
        except Exception:
            return False
        finally:
            await client.aclose()

    try:
        return asyncio.run(_check())
    except Exception:
        return False


_REDIS_OK = _redis_disponible()


@unittest.skipUnless(
    _REDIS_OK,
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo la integración real de "
    "Momento 3. Levante un Redis local (ej. `docker run --rm -p 6379:6379 "
    "redis:7-alpine`) para ejecutarla."
)
class TestMomento3IntegracionRealRedis(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        final_checkpoint_patch = patch('app.services.momento_3_sync.get_redis_client', return_value=self.redis)
        final_checkpoint_patch.start()
        self.addCleanup(final_checkpoint_patch.stop)

        self.sfc_client_mock = MagicMock()
        self.s3_client_mock = MagicMock()
        from botocore.exceptions import ClientError
        self.s3_client_mock.head_object.side_effect = ClientError({'Error':{'Code':'404'}},'HeadObject')

        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock

        self._patcher_redis = patch("app.api.routes_quejas.get_redis_client", return_value=self.redis)
        self._patcher_redis.start()

        self.smart_code = "16551509974609"

        hoy_bogota = datetime.now(ZoneInfo("America/Bogota"))
        fecha_creacion_reciente = (hoy_bogota - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")

        self.payload_tramite = {
            "Smart_Code__c": self.smart_code,
            "CreatedDate": fecha_creacion_reciente,
            "Status": "In Progress",
            "SuppliedName": "Camila Salas",
            "SC_id_type__c": "CC",
            "id_number__c": "1040011014",
            "sc_genero__c": "Femenino",
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": "No",
            "sc_Condicion_especial__c": "No aplica",
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "camila@test.com",
            "direccion__c": "Calle 93 # 11-11",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "canal__c": "Internet",
            "punto_recepcion": "Manual",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "admision_col__c": "Queja o reclamo admitida por el DCF",
            "Description": "Prueba de integración real de Momento 3.",
            "smart_anexo_queja__c": False,
            "Tutela__c": "No",
            "Ente_de_control__c": "Otros",
            "smart_escalamiento_DCF__c": "No",
            "Product__c": "Cuenta perfil",
            "smart_Producto_nombre__c": "Ahorro",
            "Categorias_COL__c": "Transacción no reconocida",
            "producto_digital__c": "Si",
            "archivos_s3": []
        }

        self._transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self._transport, base_url="http://test")
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        self._patcher_redis.stop()
        await self.redis.flushdb()
        await self.redis.aclose()

    def _smart_code_y_hash_reales(self) -> tuple[str, dict]:
        """
        El validador de QuejaUnificadaCrmInput antepone un prefijo normativo a
        Smart_Code__c -- el smart_code y el raw_payload que el servidor
        realmente usa para construir la key de idempotencia (ver
        despachar_queja_crm: `raw_payload = payload.model_dump(by_alias=True,
        mode="json")`, DESPUÉS de la validación) no son los que se mandaron en
        el request. Se reconstruyen aquí pasando por el mismo schema, para que
        la key calculada en el test coincida exactamente con la del servidor.
        """
        payload_validado = QuejaUnificadaCrmInput(**self.payload_tramite)
        raw_payload_real = payload_validado.model_dump(by_alias=True, mode="json")
        return payload_validado.Smart_Code__c, raw_payload_real

    async def _idem_key(self) -> str:
        smart_code_real, raw_payload_real = self._smart_code_y_hash_reales()
        return IdempotencyService(self.redis)._get_idempotency_key(
            smart_code_real,
            IdempotencyService.infer_operation_type(raw_payload_real),
            IdempotencyService.compute_payload_hash(raw_payload_real)
        )

    async def test_tramite_exitoso_de_punta_a_punta_persiste_idempotencia_real_y_libera_el_lock(self):
        """
        Camino feliz simple, pero con TODO lo de infraestructura real detrás:
        el registro de idempotencia debe quedar COMPLETED en Redis real (no un
        mock), y el RedisLock del caso debe quedar liberado (sin key residual)
        al terminar -- ninguna de las dos cosas se verifica hoy en ningún test
        HTTP existente de Momento 3.
        """
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"Status": "updated"})

        response = await self.client.post("/api/v1/quejas/sync/despacho", json=self.payload_tramite)

        self.assertEqual(response.status_code, 200, msg=response.text)
        self.sfc_client_mock.put_actualizar_queja.assert_called_once()

        raw_idem = await self.redis.get(await self._idem_key())
        self.assertIsNotNone(raw_idem, "El registro de idempotencia debe existir en Redis real")
        self.assertEqual(json.loads(raw_idem)["status"], "COMPLETED")

        # El lock por caso (hallazgo E) debe quedar liberado -- sin key residual.
        smart_code_real, _ = self._smart_code_y_hash_reales()
        lock_key = f"{DESPACHO_LOCK_PREFIX}:lock:{smart_code_real}"
        self.assertIsNone(await self.redis.get(lock_key))

    async def test_self_healing_m2_a_m3_de_punta_a_punta_con_redis_real(self):
        """
        La SFC rechaza el PATCH inicial porque el caso NO existe todavía
        (404/NOT_FOUND_ERROR) -- el orquestador REAL debe reaccionar creando la
        queja vía Momento 2 (post_nueva_queja) y reintentando Momento 3
        (put_actualizar_queja) automáticamente, sin que el CRM se entere. Nunca
        antes probado con Redis real detrás del idempotency lock que envuelve
        toda esta secuencia (mantener_processing_vivo).
        """
        error_no_encontrado = SfcIntegrationException(
            404, "NOT_FOUND_ERROR", None,
            f"Object with codigo_queja={self.smart_code} does not exist.",
            "Crear la queja primero"
        )
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(
            side_effect=[error_no_encontrado, {"Status": "updated"}]
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"Status": "created"})

        response = await self.client.post("/api/v1/quejas/sync/despacho", json=self.payload_tramite)

        self.assertEqual(response.status_code, 200, msg=response.text)
        self.sfc_client_mock.post_nueva_queja.assert_called_once()
        self.assertEqual(self.sfc_client_mock.put_actualizar_queja.await_count, 2)

        raw_idem = await self.redis.get(await self._idem_key())
        self.assertEqual(json.loads(raw_idem)["status"], "COMPLETED")

    async def test_mismo_payload_reenviado_usa_la_cache_de_idempotencia_real_sin_llamar_de_nuevo_a_la_sfc(self):
        """
        Confirma el corte real por idempotencia (Redis real, no mockeado): el
        MISMO payload (mismo hash) enviado dos veces sólo debe llegarle a la SFC
        una vez -- la segunda respuesta sale del registro COMPLETED en Redis.
        """
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"Status": "updated"})

        primera = await self.client.post("/api/v1/quejas/sync/despacho", json=self.payload_tramite)
        segunda = await self.client.post("/api/v1/quejas/sync/despacho", json=self.payload_tramite)

        self.assertEqual(primera.status_code, 200, msg=primera.text)
        self.assertEqual(segunda.status_code, 200, msg=segunda.text)
        self.assertEqual(segunda.headers.get("X-Idempotent-Hit"), "true")
        self.sfc_client_mock.put_actualizar_queja.assert_called_once()


if __name__ == "__main__":
    unittest.main()
