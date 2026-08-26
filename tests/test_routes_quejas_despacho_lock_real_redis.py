# tests/test_routes_quejas_despacho_lock_real_redis.py
"""
Auditoría de concurrencia (2026-08-26): el lock por caso del hallazgo E
(routes_quejas.py::despachar_queja_crm, vía RedisLock) nunca se había probado bajo
concurrencia real de punta a punta -- TestDespachoLockPorCaso en
test_routes_quejas_despacho.py mockea RedisLock por completo (acquire() siempre
devuelve un valor fijo True/False), así que nunca ejercita el SETNX real contra Redis
ni prueba que dos requests HTTP genuinamente simultáneos para el mismo Smart_Code__c
efectivamente se serialicen.

Este test dispara dos requests async REALMENTE concurrentes (httpx.AsyncClient +
ASGITransport + asyncio.gather, no el TestClient síncrono que usa el resto de la
suite) contra la app completa, con Redis real detrás de IdempotencyService, QueueService
y RedisLock -- sólo se mockean el orquestador (para no llamar a la SFC de verdad) y los
clientes SFC/S3 inyectados por dependencia.
"""
import asyncio
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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de concurrencia real "
    "del lock de despacho. Levante un Redis local (ej. `docker run --rm -p 6379:6379 "
    "redis:7-alpine`) para ejecutarlas."
)
class TestDespachoLockPorCasoConcurrenciaReal(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()

        app.dependency_overrides[get_sfc_client] = lambda: MagicMock()
        app.dependency_overrides[get_s3_client] = lambda: MagicMock()

        self._patcher_redis = patch("app.api.routes_quejas.get_redis_client", return_value=self.redis)
        self._patcher_redis.start()

        fecha_reciente = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self.payload_base = {
            "Smart_Code__c": "16551509974777",
            "CreatedDate": fecha_reciente,
            "SuppliedName": "Camila Salas",
            "SC_id_type__c": "CC",
            "id_number__c": "1040011014",
            "tipo_de_persona__c": "B2C",
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "camila@test.com",
            "direccion__c": "Calle 93 # 11-11",
            "canal__c": "Internet",
            "punto_recepcion": "Manual",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "admision_col__c": "No Aplica",
            "Description": "Prueba de concurrencia real del lock por caso.",
            "smart_anexo_queja__c": False,
            "Tutela__c": "No",
            "Ente_de_control__c": "Otros",
            "smart_escalamiento_DCF__c": "No",
            "Product__c": "Cuenta perfil",
            "smart_Producto_nombre__c": "Ahorro",
            "Categorias_COL__c": "Transacción no reconocida",
            "archivos_s3": [],
        }

    async def asyncTearDown(self):
        self._patcher_redis.stop()
        app.dependency_overrides.clear()
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_dos_payloads_distintos_concurrentes_del_mismo_caso_solo_uno_llega_al_orquestador(self):
        """
        Dos payloads con contenido distinto (trámite vs. alta nueva) para el MISMO
        Smart_Code__c, disparados genuinamente en paralelo. La idempotencia por sí
        sola NO los bloquearía entre sí (hashes distintos -- ver
        test_idempotency_concurrencia_real_redis.py); sólo el lock por caso puede
        serializarlos. Uno debe completar de forma síncrona (200) y el otro debe
        quedar encolado por contingencia (202), y el orquestador -- que representaría
        la llamada real a la SFC -- sólo debe haberse invocado UNA vez.
        """
        payload_a = dict(self.payload_base)
        payload_a["Status"] = "New"

        payload_b = dict(self.payload_base)
        payload_b["Status"] = "In Progress"

        async def procesar_lento(*_args, **_kwargs):
            # Ensancha la ventana de la carrera para que la segunda request
            # realmente encuentre el lock tomado, en vez de sólo colarse por
            # buena suerte de scheduling.
            await asyncio.sleep(0.2)
            return {"status": "success"}

        with patch("app.api.routes_quejas.DespachoQuejaOrquestador") as mock_orq_cls, \
             patch("app.api.routes_quejas.QueueService") as mock_queue_cls:
            mock_orq_cls.return_value.procesar_despacho = AsyncMock(side_effect=procesar_lento)
            mock_queue_cls.return_value.encolar_despacho = AsyncMock(
                return_value=MagicMock(id=1, es_duplicado=False)
            )
            mock_queue_cls.return_value.cancelar_pendiente_por_smart_code = AsyncMock(return_value=False)

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.headers.update({"X-API-Key": settings.CRM_API_KEY})

                respuesta_a, respuesta_b = await asyncio.gather(
                    client.post("/api/v1/quejas/sync/despacho", json=payload_a),
                    client.post("/api/v1/quejas/sync/despacho", json=payload_b),
                )

        codigos = sorted([respuesta_a.status_code, respuesta_b.status_code])
        self.assertEqual(
            codigos, [200, 202],
            f"Uno debe completar sincrono (200) y el otro debe quedar encolado (202). "
            f"Códigos obtenidos: a={respuesta_a.status_code} b={respuesta_b.status_code}"
        )
        self.assertEqual(
            mock_orq_cls.return_value.procesar_despacho.call_count, 1,
            "El orquestador (equivalente a la llamada real a la SFC) debe invocarse UNA sola vez -- "
            "si se invocara dos, el lock por caso no está sirviendo de nada."
        )
        mock_queue_cls.return_value.encolar_despacho.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
