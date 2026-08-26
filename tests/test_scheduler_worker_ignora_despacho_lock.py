# tests/test_scheduler_worker_ignora_despacho_lock.py
"""
Auditoría del flujo completo de despacho/reintentos (2026-08-26): el hallazgo E
agregó un lock por Smart_Code__c (RedisLock) alrededor del despacho SÍNCRONO en
routes_quejas.py -- pero el camino del WORKER (scheduler.py, disparado por
reintentar_despachos_pendientes_job) llamaba a orquestador.procesar_despacho_raw_json
directamente, SIN adquirir ese mismo lock ni ningún otro por caso. Se probó la
brecha de forma directa (ver el primer test de este archivo, que sigue corriendo
como regresión) y se corrigió: scheduler.py::_reclamar_y_procesar_si_lock_disponible
ahora adquiere el mismo lock, con la misma llave, ANTES de reclamar el item -- si
está ocupado, el item se deja sin reclamar para el próximo ciclo en vez de competir
por la SFC. Este archivo cubre ambos lados: que el lock realmente bloquea al worker
cuando está ocupado, y que el worker sigue funcionando con normalidad cuando no lo
está.
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
from app.services.queue_service import QueueService, DESPACHO_LOCK_PREFIX
from app.core.constants import SmartStatus

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo las pruebas del lock "
    "worker/despacho. Levante un Redis local (ej. `docker run --rm -p 6379:6379 "
    "redis:7-alpine`) para ejecutarlas."
)
class TestReclamarYProcesarSiLockDisponible(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_lock_ocupado_no_reclama_el_item_lo_deja_para_el_siguiente_ciclo(self):
        """Regresión directa del hallazgo: con el lock de despacho tomado (simulando
        un request síncrono en vuelo del mismo caso), el worker ya NO debe reclamar
        el item -- debe quedar exactamente como estaba, listo para el próximo ciclo."""
        from app.workers.scheduler import _reclamar_y_procesar_si_lock_disponible

        smart_code = "SC-GAP-1"
        item = await self.queue_service.encolar_despacho(
            smart_code=smart_code, tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": smart_code, "Status": "In Progress"},
            error_inicial="timeout inicial"
        )

        despacho_lock = RedisLock(
            redis_client=self.redis,
            lock_key=f"{DESPACHO_LOCK_PREFIX}:lock:{smart_code}",
            lease_segundos=180
        )
        self.assertTrue(await despacho_lock.acquire())

        try:
            orquestador_mock = MagicMock()
            orquestador_mock.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            resultado = await _reclamar_y_procesar_si_lock_disponible(
                self.redis, self.queue_service, orquestador_mock, item, "worker_1"
            )

            self.assertIsNone(resultado)
            orquestador_mock.procesar_despacho_raw_json.assert_not_called()
            # El item nunca se reclamó: sigue PENDIENTE, sin claim.
            self.assertIsNone(await self.redis.get(f"{{sfc:queue}}:claim:{item.id}"))
            registros = await self.queue_service.obtener_todos_los_encolados(estado="PENDIENTE")
            self.assertEqual([r.id for r in registros], [item.id])
        finally:
            await despacho_lock.release()

    async def test_lock_libre_reclama_y_procesa_con_normalidad(self):
        from app.workers.scheduler import _reclamar_y_procesar_si_lock_disponible

        smart_code = "SC-GAP-2"
        item = await self.queue_service.encolar_despacho(
            smart_code=smart_code, tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": smart_code, "Status": "In Progress"},
            error_inicial="timeout inicial"
        )

        with patch(
            "app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia",
            new_callable=AsyncMock, return_value=(True, None)
        ):
            orquestador_mock = MagicMock()
            orquestador_mock.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            resultado = await _reclamar_y_procesar_si_lock_disponible(
                self.redis, self.queue_service, orquestador_mock, item, "worker_1"
            )

        self.assertIsNotNone(resultado)
        self.assertTrue(resultado.despachado_exito)
        orquestador_mock.procesar_despacho_raw_json.assert_awaited_once()
        # El lock se liberó -- otro actor podría adquirirlo de inmediato.
        lock_libre = RedisLock(
            redis_client=self.redis, lock_key=f"{DESPACHO_LOCK_PREFIX}:lock:{smart_code}"
        )
        self.assertTrue(await lock_libre.acquire())
        await lock_libre.release()

    async def test_sync_y_worker_genuinamente_concurrentes_solo_uno_procede(self):
        """
        Concurrencia real (no simulada secuencialmente): un request síncrono y un
        reintento del worker para el MISMO caso, disparados con asyncio.gather.
        Exactamente uno debe llegar a "llamar a la SFC" -- el otro debe encontrar
        el lock ocupado y ceder sin competir.
        """
        from app.workers.scheduler import _reclamar_y_procesar_si_lock_disponible

        smart_code = "SC-GAP-3"
        item = await self.queue_service.encolar_despacho(
            smart_code=smart_code, tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": smart_code, "Status": "In Progress"},
            error_inicial="timeout inicial"
        )

        llamadas_a_sfc = []

        async def lado_sincrono():
            lock = RedisLock(
                redis_client=self.redis, lock_key=f"{DESPACHO_LOCK_PREFIX}:lock:{smart_code}",
                lease_segundos=180
            )
            if not await lock.acquire():
                return "perdio"
            try:
                await asyncio.sleep(0.15)  # simula la llamada real a la SFC
                llamadas_a_sfc.append("sincrono")
                return "gano"
            finally:
                await lock.release()

        async def lado_worker():
            orquestador_mock = MagicMock()

            async def _procesar_lento(*_a, **_k):
                await asyncio.sleep(0.15)
                llamadas_a_sfc.append("worker")
                return {"status": "success"}

            orquestador_mock.procesar_despacho_raw_json = AsyncMock(side_effect=_procesar_lento)
            with patch(
                "app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia",
                new_callable=AsyncMock, return_value=(True, None)
            ):
                resultado = await _reclamar_y_procesar_si_lock_disponible(
                    self.redis, self.queue_service, orquestador_mock, item, "worker_1"
                )
            return "gano" if resultado is not None else "perdio"

        resultado_sync, resultado_worker = await asyncio.gather(lado_sincrono(), lado_worker())

        ganadores = [r for r in (resultado_sync, resultado_worker) if r == "gano"]
        self.assertEqual(len(ganadores), 1, "Exactamente un lado debe ganar el lock y proceder")
        self.assertEqual(
            len(llamadas_a_sfc), 1,
            f"Sólo debió haber una 'llamada a la SFC' -- se registraron: {llamadas_a_sfc}"
        )


if __name__ == "__main__":
    unittest.main()
