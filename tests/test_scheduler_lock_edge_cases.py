# tests/test_scheduler_lock_edge_cases.py
"""
Cobertura de ramas de SchedulerJobLock no cubiertas por test_scheduler_lock.py:
el heartbeat detectando que perdió el lock, su manejo de errores de Redis,
release() como no-op si nunca se adquirió, el error al liberar, y el uso
como context manager async (__aenter__/__aexit__).
"""
import asyncio
import unittest
from unittest.mock import AsyncMock

from app.workers.scheduler import SchedulerJobLock


class TestSchedulerJobLockHeartbeatEdgeCases(unittest.IsolatedAsyncioTestCase):

    async def test_heartbeat_detecta_perdida_del_lock_y_deja_de_estar_acquired(self):
        redis_mock = AsyncMock()
        redis_mock.set.return_value = True
        redis_mock.eval.return_value = 0  # El script CAD no encontró el owner_token esperado.

        lock = SchedulerJobLock(
            redis_client=redis_mock, lock_key="{sfc:scheduler}:lock:test", intervalo_heartbeat=0.01
        )
        await lock.acquire()
        await asyncio.sleep(0.05)  # deja correr el heartbeat un par de veces

        # El loop hace `break` en cuanto detecta la pérdida -- no queda reintentando indefinidamente.
        self.assertTrue(lock._heartbeat_task.done())
        await lock.release()

    async def test_heartbeat_error_de_redis_no_interrumpe_el_loop(self):
        redis_mock = AsyncMock()
        redis_mock.set.return_value = True
        redis_mock.eval.side_effect = ConnectionError("redis caido")

        lock = SchedulerJobLock(
            redis_client=redis_mock, lock_key="{sfc:scheduler}:lock:test", intervalo_heartbeat=0.01
        )
        await lock.acquire()
        await asyncio.sleep(0.05)

        self.assertGreaterEqual(redis_mock.eval.await_count, 2)  # sigue reintentando pese al error.
        lock.acquired = False  # corta el loop para el cleanup del test.
        await lock.release()


class TestSchedulerJobLockRelease(unittest.IsolatedAsyncioTestCase):

    async def test_release_sin_haber_adquirido_es_no_op(self):
        redis_mock = AsyncMock()
        lock = SchedulerJobLock(redis_client=redis_mock, lock_key="{sfc:scheduler}:lock:test")

        await lock.release()  # No debe lanzar ni llamar a Redis.

        redis_mock.eval.assert_not_called()

    async def test_release_con_error_de_redis_no_propaga(self):
        redis_mock = AsyncMock()
        redis_mock.set.return_value = True
        redis_mock.eval.side_effect = ConnectionError("redis caido")

        lock = SchedulerJobLock(
            redis_client=redis_mock, lock_key="{sfc:scheduler}:lock:test", intervalo_heartbeat=10
        )
        await lock.acquire()

        await lock.release()  # No debe lanzar aunque el CAD de liberación falle.

        self.assertFalse(lock.acquired)


class TestSchedulerJobLockContextManager(unittest.IsolatedAsyncioTestCase):

    async def test_async_with_adquiere_y_libera_automaticamente(self):
        redis_mock = AsyncMock()
        redis_mock.set.return_value = True
        redis_mock.eval.return_value = 1

        async with SchedulerJobLock(
            redis_client=redis_mock, lock_key="{sfc:scheduler}:lock:test", intervalo_heartbeat=10
        ) as lock:
            self.assertTrue(lock.acquired)

        self.assertFalse(lock.acquired)
        redis_mock.eval.assert_awaited()  # el CAD de liberación se ejecutó al salir del bloque.


if __name__ == "__main__":
    unittest.main()
