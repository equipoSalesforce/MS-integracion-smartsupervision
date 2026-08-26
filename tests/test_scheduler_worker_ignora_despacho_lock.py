# tests/test_scheduler_worker_ignora_despacho_lock.py
"""
Auditoría del flujo completo de despacho/reintentos (2026-08-26): el hallazgo E
agregó un lock por Smart_Code__c (RedisLock) alrededor del despacho SÍNCRONO en
routes_quejas.py -- pero el camino del WORKER (scheduler.py::_ejecutar_paso_sfc,
disparado por reintentar_despachos_pendientes_job) llama a
orquestador.procesar_despacho_raw_json directamente, SIN adquirir ese mismo lock ni
ningún otro por caso.

Esto significa que el lock de E sólo protege contra dos requests SÍNCRONOS
concurrentes del mismo caso -- no contra un request síncrono nuevo que llega mientras
el worker está reintentando en background un item YA encolado del MISMO
Smart_Code__c (escenario real: la SFC estuvo caída, un evento quedó en cola; la SFC
se recupera; el scheduler dispara su próximo ciclo y reclama ese item justo cuando,
independientemente, llega un evento nuevo del mismo caso por la vía síncrona). Este
test prueba la brecha de forma directa y quirúrgica: adquiere el lock exactamente
como lo haría un request síncrono en vuelo, y confirma que el código real del
worker (_ejecutar_paso_sfc) igual procede sin bloquearse.
"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.core.distributed_lock import RedisLock
from app.services.queue_service import QueueService
from app.api.routes_quejas import DESPACHO_LOCK_PREFIX

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo la prueba de la brecha "
    "worker/lock. Levante un Redis local (ej. `docker run --rm -p 6379:6379 "
    "redis:7-alpine`) para ejecutarla."
)
class TestWorkerIgnoraDespachoLock(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_worker_procesa_el_item_pese_a_que_el_lock_de_despacho_esta_tomado(self):
        from app.workers.scheduler import _ejecutar_paso_sfc, _ResultadoItemReintento

        smart_code = "SC-GAP-1"
        item = await self.queue_service.encolar_despacho(
            smart_code=smart_code, tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": smart_code, "Status": "In Progress"},
            error_inicial="timeout inicial"
        )
        claim = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        self.assertIsNotNone(claim)

        # Simula un request SÍNCRONO en vuelo para el MISMO caso: adquiere el lock
        # exacto que routes_quejas.py usaría.
        despacho_lock = RedisLock(
            redis_client=self.redis,
            lock_key=f"{DESPACHO_LOCK_PREFIX}:lock:{smart_code}",
            lease_segundos=180
        )
        self.assertTrue(await despacho_lock.acquire())

        try:
            orquestador_mock = MagicMock()
            orquestador_mock.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})
            resultado = _ResultadoItemReintento()

            # Código REAL del worker, sin ningún mock del propio mecanismo de lock --
            # si el worker respetara el lock de despacho, esto debería bloquearse o
            # negarse mientras despacho_lock sigue tomado. En cambio, procede normal.
            continuar, resultado_sfc = await _ejecutar_paso_sfc(
                orquestador_mock, self.queue_service, claim, "worker_1",
                claim.payload_json, resultado
            )

            self.assertTrue(
                continuar,
                "El worker completó el paso SFC con éxito pese a que el lock de "
                "despacho del mismo caso seguía tomado por otro actor -- confirma "
                "que el camino del worker no participa en ese lock."
            )
            orquestador_mock.procesar_despacho_raw_json.assert_awaited_once()
        finally:
            await despacho_lock.release()


if __name__ == "__main__":
    unittest.main()
