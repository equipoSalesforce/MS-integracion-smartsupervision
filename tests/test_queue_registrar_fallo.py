# tests/test_queue_registrar_fallo.py
"""
Prueba de integración contra Redis REAL de QueueService.registrar_fallo -- la
transición que decide si un item pendiente se reintenta o pasa a
FALLIDO_DEFINITIVO (DLQ). Antes sin cobertura directa de la rama DLQ
(notificar_caso_fallido_definitivo + liberación del registro de idempotencia)
ni de consumir_intento=False (fallo del webhook al CRM por infraestructura,
que no debe agotar el mismo presupuesto de reintentos que un fallo real de
despacho -- ver scheduler.py::_es_falla_infraestructura).

Mismo criterio que test_queue_race_protection.py / test_queue_purga_registros.py:
contra Redis real, no un mock del script Lua.
"""
import os
import unittest
from unittest.mock import patch, AsyncMock

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, QUEUE_PREFIX
from app.core.config import settings

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


def _redis_disponible() -> bool:
    if redis_asyncio is None:
        return False
    import asyncio

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de integración de registrar_fallo. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarlas."
)
class TestRegistrarFallo(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def _encolar_y_reclamar(self, smart_code: str, worker_id: str = "worker_1"):
        item = await self.queue_service.encolar_despacho(
            smart_code=smart_code, tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": smart_code}, error_inicial="timeout inicial"
        )
        return await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_id, lease_segundos=60
        )

    async def test_fallo_normal_incrementa_intentos_y_sigue_pendiente(self):
        with patch.object(settings, "QUEUE_MAX_RETRIES", 5):
            item_reclamado = await self._encolar_y_reclamar("SC-1")
            with patch("app.services.queue_service.EmailAlertService.notificar_caso_fallido_definitivo", new_callable=AsyncMock) as mock_alert:
                resultado = await self.queue_service.registrar_fallo(
                    item=item_reclamado, error_msg="SFC timeout", worker_id="worker_1"
                )

        self.assertEqual(resultado, "failed")
        mock_alert.assert_not_called()
        self.assertEqual(await self.queue_service.contar_pendientes(), 1)

    async def test_alcanza_max_intentos_pasa_a_dlq_y_libera_idempotencia(self):
        with patch.object(settings, "QUEUE_MAX_RETRIES", 1):
            item_reclamado = await self._encolar_y_reclamar("SC-2")
            with patch("app.services.queue_service.EmailAlertService.notificar_caso_fallido_definitivo", new_callable=AsyncMock) as mock_alert:
                resultado = await self.queue_service.registrar_fallo(
                    item=item_reclamado, error_msg="SFC caída sostenida", worker_id="worker_1"
                )

        self.assertEqual(resultado, "failed")
        mock_alert.assert_awaited_once()
        self.assertEqual(mock_alert.call_args.kwargs["smart_code"], "SC-2")
        self.assertEqual(await self.queue_service.contar_pendientes(), 0)
        self.assertEqual(
            await self.redis.scard(f"{QUEUE_PREFIX}:status:FALLIDO_DEFINITIVO"), 1
        )

    async def test_consumir_intento_false_no_incrementa_ni_marca_definitivo(self):
        with patch.object(settings, "QUEUE_MAX_RETRIES", 1):
            item_reclamado = await self._encolar_y_reclamar("SC-3")
            with patch("app.services.queue_service.EmailAlertService.notificar_caso_fallido_definitivo", new_callable=AsyncMock) as mock_alert:
                resultado = await self.queue_service.registrar_fallo(
                    item=item_reclamado, error_msg="webhook CRM caído", worker_id="worker_1",
                    consumir_intento=False
                )

        self.assertEqual(resultado, "failed")
        mock_alert.assert_not_called()
        # Sigue pendiente pese a QUEUE_MAX_RETRIES=1: consumir_intento=False no cuenta como intento.
        self.assertEqual(await self.queue_service.contar_pendientes(), 1)
        self.assertEqual(
            await self.redis.scard(f"{QUEUE_PREFIX}:status:FALLIDO_DEFINITIVO"), 0
        )


class TestRegistrarFalloSinRedis(unittest.IsolatedAsyncioTestCase):

    async def test_sin_cliente_redis_retorna_not_found_sin_lanzar(self):
        queue_service = QueueService(redis_client=None)

        class _ItemFake:
            id = 1

        resultado = await queue_service.registrar_fallo(item=_ItemFake(), error_msg="x", worker_id="w1")
        self.assertEqual(resultado, "not_found")


if __name__ == "__main__":
    unittest.main()
