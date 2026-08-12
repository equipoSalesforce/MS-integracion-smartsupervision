# tests/test_queue_lock_watchdog.py
import json
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from app.services.queue_service import QueueService
from app.workers.scheduler import QueueLockWatchdog


class TestQueueLockWatchdog(unittest.IsolatedAsyncioTestCase):

    async def test_watchdog_renueva_lease_exitosamente(self):
        """
        1. Valida que una tarea de larga duración renueve el bloqueo varias veces 
           antes de expirar, manteniendo la propiedad del ítem.
        """
        mock_redis = AsyncMock()
        queue_service = QueueService(redis_client=mock_redis)

        mock_redis.eval = AsyncMock(return_value=json.dumps({"extended": True}))

        registro_id = 100
        worker_id = "worker_node_alpha"

        async with QueueLockWatchdog(
            queue_service=queue_service,
            registro_id=registro_id,
            worker_id=worker_id,
            lease_segundos=2,
            intervalo_segundos=0.1
        ):
            await asyncio.sleep(0.35)

        self.assertGreaterEqual(mock_redis.eval.call_count, 3)
        
        call_args = mock_redis.eval.call_args[0]
        # 🟢 Aserción corregida con dos puntos
        self.assertIn("{sfc:queue}:claim:100", call_args[2])
        self.assertEqual(call_args[3], worker_id)

    async def test_watchdog_se_cancela_al_salir_del_contexto(self):
        """
        2. Valida que al salir del bloque 'async with', la tarea en segundo plano 
           del Watchdog se cancele inmediatamente y no siga enviando pings a Redis.
        """
        mock_redis = AsyncMock()
        queue_service = QueueService(redis_client=mock_redis)
        mock_redis.eval = AsyncMock(return_value=json.dumps({"extended": True}))

        watchdog = QueueLockWatchdog(
            queue_service=queue_service,
            registro_id=101,
            worker_id="worker_node_alpha",
            lease_segundos=2,
            intervalo_segundos=0.05
        )

        async with watchdog:
            await asyncio.sleep(0.08)

        llamadas_al_salir = mock_redis.eval.call_count
        await asyncio.sleep(0.15)

        self.assertEqual(mock_redis.eval.call_count, llamadas_al_salir)
        self.assertTrue(watchdog._task.done())

    async def test_watchdog_se_detiene_si_pierde_propiedad_del_candado(self):
        """
        3. Valida que si el script Lua informa que el candado expiro o fue asignado 
           a otro worker, el Watchdog aborte su bucle de reintentos de inmediato.
        """
        mock_redis = AsyncMock()
        queue_service = QueueService(redis_client=mock_redis)

        mock_redis.eval = AsyncMock(return_value=json.dumps({
            "extended": False, 
            "reason": "owner_mismatch_or_expired"
        }))

        async with QueueLockWatchdog(
            queue_service=queue_service,
            registro_id=102,
            worker_id="worker_node_beta",
            lease_segundos=2,
            intervalo_segundos=0.05
        ):
            await asyncio.sleep(0.20)

        self.assertEqual(mock_redis.eval.call_count, 1)

    async def test_extender_lease_lua_script_integracion_redis(self):
        """
        4. Prueba de integración atómica del script Lua directamente sobre Redis.
        """
        from app.db.redis import get_redis_client
        redis = get_redis_client()

        if not redis:
            self.skipTest("Servidor Redis no disponible para prueba de integración.")

        queue_service = QueueService(redis)
        registro_id = 9999
        worker_id_prop = "worker_propietario"
        claim_key = f"{{sfc:queue}}:claim:{registro_id}"

        try:
            await redis.set(claim_key, worker_id_prop, px=2000)

            exito = await queue_service.extender_lease_item(
                registro_id=registro_id,
                worker_id=worker_id_prop,
                lease_segundos=10
            )
            self.assertTrue(exito)

            ttl_ms = await redis.pttl(claim_key)
            self.assertGreater(ttl_ms, 2000)

            exito_intruso = await queue_service.extender_lease_item(
                registro_id=registro_id,
                worker_id="worker_intruso",
                lease_segundos=10
            )
            self.assertFalse(exito_intruso)

        finally:
            await redis.delete(claim_key)


if __name__ == "__main__":
    unittest.main()