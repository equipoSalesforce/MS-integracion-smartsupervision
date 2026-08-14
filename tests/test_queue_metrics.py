# tests/test_queue_metrics.py
import time
import unittest

from app.services.queue_service import QueueService


class _StubRedisAntiguedad:
    """
    Stub mínimo de las operaciones de Redis que usa
    obtener_edad_item_mas_antiguo_pendiente: zrangebyscore(withscores=True) sobre
    created_zset (se asume ya ordenado ascendente por score, como en Redis real)
    y sismember contra el set de status PENDING.
    """

    def __init__(self, zset_entries, pending_ids, fallar=False):
        self._zset_entries = sorted(zset_entries, key=lambda e: e[1])
        self._pending_ids = set(pending_ids)
        self._fallar = fallar

    async def zrangebyscore(self, key, min, max, start=None, num=None, withscores=False):
        if self._fallar:
            raise ConnectionError("Redis caído")
        entries = self._zset_entries
        if start is not None and num is not None:
            entries = entries[start:start + num]
        return entries if withscores else [e[0] for e in entries]

    async def sismember(self, key, member):
        return member in self._pending_ids


class TestObtenerEdadItemMasAntiguoPendiente(unittest.IsolatedAsyncioTestCase):

    async def test_devuelve_antiguedad_del_mas_antiguo_pendiente(self):
        ahora = time.time()
        redis_stub = _StubRedisAntiguedad(
            zset_entries=[("1", ahora - 500), ("2", ahora - 100)],
            pending_ids={"1", "2"}
        )
        service = QueueService(redis_client=redis_stub)

        edad = await service.obtener_edad_item_mas_antiguo_pendiente()

        self.assertIsNotNone(edad)
        self.assertAlmostEqual(edad, 500, delta=2)

    async def test_salta_items_que_ya_no_estan_pendientes(self):
        """El más antiguo del zset ya no está PENDING (completado/DLQ); debe usar el siguiente."""
        ahora = time.time()
        redis_stub = _StubRedisAntiguedad(
            zset_entries=[("1", ahora - 900), ("2", ahora - 200)],
            pending_ids={"2"}
        )
        service = QueueService(redis_client=redis_stub)

        edad = await service.obtener_edad_item_mas_antiguo_pendiente()

        self.assertIsNotNone(edad)
        self.assertAlmostEqual(edad, 200, delta=2)

    async def test_devuelve_none_si_cola_vacia(self):
        redis_stub = _StubRedisAntiguedad(zset_entries=[], pending_ids=set())
        service = QueueService(redis_client=redis_stub)

        edad = await service.obtener_edad_item_mas_antiguo_pendiente()

        self.assertIsNone(edad)

    async def test_devuelve_none_si_ningun_candidato_sigue_pendiente(self):
        ahora = time.time()
        redis_stub = _StubRedisAntiguedad(
            zset_entries=[("1", ahora - 900), ("2", ahora - 200)],
            pending_ids=set()
        )
        service = QueueService(redis_client=redis_stub)

        edad = await service.obtener_edad_item_mas_antiguo_pendiente()

        self.assertIsNone(edad)

    async def test_devuelve_none_si_redis_no_configurado(self):
        service = QueueService(redis_client=None)

        edad = await service.obtener_edad_item_mas_antiguo_pendiente()

        self.assertIsNone(edad)

    async def test_no_lanza_si_redis_falla(self):
        redis_stub = _StubRedisAntiguedad(zset_entries=[], pending_ids=set(), fallar=True)
        service = QueueService(redis_client=redis_stub)

        edad = await service.obtener_edad_item_mas_antiguo_pendiente()

        self.assertIsNone(edad)


if __name__ == "__main__":
    unittest.main()
