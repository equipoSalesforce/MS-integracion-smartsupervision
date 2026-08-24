# tests/test_idempotency_heartbeat.py
"""
Cobertura de IdempotencyProcessingHeartbeat (app/services/idempotency_service.py)
-- el context manager que renueva periodicamente el candado PROCESSING de
idempotencia mientras dura la llamada real a la SFC, en vez de depender de un
TTL fijo que podria expirar antes de que termine el despacho. Antes sin
cobertura directa.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.services.idempotency_service import IdempotencyProcessingHeartbeat


class TestIdempotencyProcessingHeartbeat(unittest.IsolatedAsyncioTestCase):

    async def test_sin_redis_no_crea_tarea_en_segundo_plano(self):
        heartbeat = IdempotencyProcessingHeartbeat(redis_client=None, key="k", payload_hash="h")
        async with heartbeat:
            self.assertIsNone(heartbeat._task)

    async def test_renueva_el_lease_periodicamente(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=1)

        async with IdempotencyProcessingHeartbeat(
            redis_client=mock_redis, key="idem:k1", payload_hash="hash123", intervalo_segundos=0.01
        ):
            await asyncio.sleep(0.05)  # deja correr un par de iteraciones del heartbeat

        self.assertGreaterEqual(mock_redis.eval.await_count, 2)
        posicionales = mock_redis.eval.call_args[0]
        self.assertEqual(posicionales[2], "idem:k1")
        self.assertEqual(posicionales[3], "hash123")

    async def test_fallo_al_renovar_no_interrumpe_el_loop_ni_propaga(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis caído"))

        async with IdempotencyProcessingHeartbeat(
            redis_client=mock_redis, key="idem:k2", payload_hash="hash456", intervalo_segundos=0.01
        ):
            await asyncio.sleep(0.05)  # No debe lanzar aunque cada renovación falle.

        self.assertGreaterEqual(mock_redis.eval.await_count, 2)

    async def test_al_salir_cancela_la_tarea_en_segundo_plano(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=1)

        heartbeat = IdempotencyProcessingHeartbeat(
            redis_client=mock_redis, key="idem:k3", payload_hash="hash789", intervalo_segundos=5
        )
        async with heartbeat:
            tarea = heartbeat._task
            self.assertIsNotNone(tarea)
            self.assertFalse(tarea.done())

        self.assertTrue(tarea.cancelled() or tarea.done())


if __name__ == "__main__":
    unittest.main()
