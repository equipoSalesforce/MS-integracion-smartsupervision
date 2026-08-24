# tests/test_scheduler_lock.py
import asyncio
import unittest
from unittest.mock import AsyncMock
from app.workers.scheduler import SchedulerJobLock, RELEASE_LOCK_LUA_SCRIPT, EXTEND_LOCK_LUA_SCRIPT


class TestSchedulerJobLock(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.redis_mock = AsyncMock()

    async def test_acquire_lock_exitoso_genera_token_y_heartbeat(self):
        """Verifica que al adquirir el lock se asigne un token único y se inicie la tarea de heartbeat."""
        self.redis_mock.set.return_value = True

        lock = SchedulerJobLock(
            redis_client=self.redis_mock,
            lock_key="{sfc:scheduler}:lock:test_job",
            lease_segundos=10,
            intervalo_heartbeat=1
        )

        acquired = await lock.acquire()

        self.assertTrue(acquired)
        self.assertTrue(lock.acquired)
        self.assertIsNotNone(lock.owner_token)
        self.assertIsNotNone(lock._heartbeat_task)

        # Verificamos que SET en Redis fue invocado con el token y parámetros NX / EX
        self.redis_mock.set.assert_called_once_with(
            "{sfc:scheduler}:lock:test_job",
            lock.owner_token,
            nx=True,
            ex=10
        )

        await lock.release()

    async def test_release_lock_ejecuta_script_lua_cad(self):
        """Verifica que la liberación del candado invoque el script Lua Compare-And-Delete (CAD)."""
        self.redis_mock.set.return_value = True
        self.redis_mock.eval.return_value = 1

        lock = SchedulerJobLock(
            redis_client=self.redis_mock,
            lock_key="{sfc:scheduler}:lock:test_job",
            lease_segundos=10,
            intervalo_heartbeat=1
        )

        await lock.acquire()
        token = lock.owner_token
        await lock.release()

        self.assertFalse(lock.acquired)
        # Verificamos que el script Lua de liberación CAD fue invocado pasando la clave y el token de dueño
        self.redis_mock.eval.assert_called_with(
            RELEASE_LOCK_LUA_SCRIPT,
            1,
            "{sfc:scheduler}:lock:test_job",
            token
        )

    async def test_heartbeat_renueva_lease_en_intervalo(self):
        """Verifica que el heartbeat en segundo plano ejecute el script Lua de extensión de TTL."""
        self.redis_mock.set.return_value = True
        self.redis_mock.eval.return_value = 1

        lock = SchedulerJobLock(
            redis_client=self.redis_mock,
            lock_key="{sfc:scheduler}:lock:test_job",
            lease_segundos=2,
            intervalo_heartbeat=0.1
        )

        await lock.acquire()
        await asyncio.sleep(0.25)  # Permite que el bucle de heartbeat ejecute al menos 2 renovaciones
        await lock.release()

        # Verifica que eval fue llamado con el script de extensión
        self.redis_mock.eval.assert_any_call(
            EXTEND_LOCK_LUA_SCRIPT,
            1,
            "{sfc:scheduler}:lock:test_job",
            lock.owner_token,
            "2000"
        )


if __name__ == "__main__":
    unittest.main()