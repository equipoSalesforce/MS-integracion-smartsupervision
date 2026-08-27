# tests/test_scheduler_purga_ciclo_completo_real_redis.py
"""
Cobertura de integración de punta a punta para `scheduler.py::purgar_cola_job`
-- el job nocturno que APScheduler invoca a medianoche hora Bogotá. Mismo hueco
que ya se cerró para `reintentar_despachos_pendientes_job`: existían dos
categorías de cobertura separadas, ninguna de punta a punta.

  - `test_scheduler_lifecycle.py::TestPurgarColaJob`: llama a la función real,
    pero con `RedisLock.acquire/release` y `QueueService.purgar_registros_
    antiguos` completamente MOCKEADOS -- prueba que el wrapper llama a las
    piezas correctas, no que el lock real y el borrado real de Redis funcionen
    juntos.
  - `test_queue_purga_registros.py`: usa Redis real, pero llama directamente a
    `QueueService.purgar_registros_antiguos` -- nunca pasa por el job_lock
    (`SCHEDULER_LOCK_PREFIX`) ni por `purgar_cola_job()` en sí.

Se agrega aquí el job REAL (`purgar_cola_job`) contra Redis REAL de punta a
punta: adquiere su lock real, borra datos reales, libera el lock real -- y el
caso en que otro nodo ya tiene el lock tomado, que hoy nadie prueba contra un
lock genuino (los tests mockeados de `RedisLock.acquire` no pueden detectar si
la KEY real que usan `reintentar_despachos_pendientes_job` y `purgar_cola_job`
llegara a colisionar por error, por ejemplo).
"""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, QUEUE_PREFIX
from app.core.constants import SmartStatus
from app.core.config import settings
from app.core.distributed_lock import RedisLock
from app.workers.scheduler import purgar_cola_job, SCHEDULER_LOCK_PREFIX

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo el ciclo completo de "
    "integración de la purga. Levante un Redis local (ej. `docker run --rm "
    "-p 6379:6379 redis:7-alpine`) para ejecutarlas."
)
class TestPurgarColaJobCicloCompletoRealRedis(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

        self._patcher_redis = patch("app.workers.scheduler.get_redis_client", return_value=self.redis)
        self._patcher_redis.start()
        self.addCleanup(self._patcher_redis.stop)

        self.lock_key = f"{SCHEDULER_LOCK_PREFIX}:lock:purge_job"

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def _crear_item_en_estado(self, item_id: int, smart_code: str, estado: str, dias_antiguedad: float) -> None:
        """Mismo helper que test_queue_purga_registros.py -- inserta directamente
        en Redis un item ya en el estado/antigüedad deseados."""
        updated_at = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(days=dias_antiguedad)).isoformat()
        item_key = f"{QUEUE_PREFIX}:item:{item_id}"
        data = {
            "id": item_id, "smart_code": smart_code, "tipo_operacion": "AUTO",
            "payload_json": {"Smart_Code__c": smart_code}, "estado": estado,
            "sfc_completado": True, "sfc_response": None, "intentos": 1, "max_intentos": 10,
            "ultimo_error": None, "proximo_reintento_at": None,
            "created_at": updated_at, "updated_at": updated_at,
            "correlation_id": "N/A", "es_duplicado": False, "version": 1,
        }
        await self.redis.set(item_key, json.dumps(data, ensure_ascii=False))
        await self.redis.sadd(f"{QUEUE_PREFIX}:status:{estado}", str(item_id))
        await self.redis.zadd(f"{QUEUE_PREFIX}:created_zset", {str(item_id): 0})

    async def test_purga_real_de_punta_a_punta_borra_lo_vencido_conserva_lo_reciente_y_libera_el_lock(self):
        """
        El job REAL, con Redis real detrás de TODO: adquiere el lock real,
        borra los registros EXITOSO/FALLIDO_DEFINITIVO vencidos según sus
        propias ventanas de retención, conserva los recientes, y libera el
        lock real al terminar -- nunca antes probado junta esta secuencia
        completa.
        """
        await self._crear_item_en_estado(1, "SC-OLD-OK", SmartStatus.COMPLETED.value, dias_antiguedad=40)
        await self._crear_item_en_estado(2, "SC-NEW-OK", SmartStatus.COMPLETED.value, dias_antiguedad=1)
        await self._crear_item_en_estado(3, "SC-OLD-DLQ", SmartStatus.FAILED_FINAL.value, dias_antiguedad=100)
        await self._crear_item_en_estado(4, "SC-NEW-DLQ", SmartStatus.FAILED_FINAL.value, dias_antiguedad=10)

        with patch.object(settings, "QUEUE_RETENTION_DAYS", 30), \
             patch.object(settings, "QUEUE_RETENTION_DAYS_DLQ", 90):
            await purgar_cola_job()

        # Vencidos: borrados por completo (item, set de estado, created_zset).
        for item_id in (1, 3):
            self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:item:{item_id}"))
            self.assertIsNone(await self.redis.zscore(f"{QUEUE_PREFIX}:created_zset", str(item_id)))
        self.assertFalse(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", "1"))
        self.assertFalse(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.FAILED_FINAL.value}", "3"))

        # Recientes: intactos.
        for item_id in (2, 4):
            self.assertIsNotNone(await self.redis.get(f"{QUEUE_PREFIX}:item:{item_id}"))
        self.assertTrue(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", "2"))
        self.assertTrue(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.FAILED_FINAL.value}", "4"))

        # El job_lock real debe quedar liberado -- sin key residual.
        self.assertIsNone(await self.redis.get(self.lock_key))

    async def test_lock_ya_tomado_por_otro_nodo_no_ejecuta_la_purga_ni_toca_los_datos(self):
        """
        Otro nodo worker ya tiene el job_lock de purga real tomado (ej. sigue
        corriendo su propio ciclo) -- este nodo debe ceder de inmediato sin
        tocar ningún registro, ni siquiera los genuinamente vencidos.
        """
        await self._crear_item_en_estado(1, "SC-OLD-OK", SmartStatus.COMPLETED.value, dias_antiguedad=40)

        lock_de_otro_nodo = RedisLock(redis_client=self.redis, lock_key=self.lock_key, lease_segundos=60)
        self.assertTrue(await lock_de_otro_nodo.acquire())

        try:
            with patch.object(settings, "QUEUE_RETENTION_DAYS", 30), \
                 patch.object(settings, "QUEUE_RETENTION_DAYS_DLQ", 90):
                await purgar_cola_job()

            # El item vencido sigue intacto -- la purga ni siquiera se intentó.
            self.assertIsNotNone(await self.redis.get(f"{QUEUE_PREFIX}:item:1"))
            self.assertTrue(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", "1"))
        finally:
            await lock_de_otro_nodo.release()

    async def test_dos_corridas_concurrentes_de_la_purga_solo_una_ejecuta(self):
        """
        Dos invocaciones genuinamente concurrentes de purgar_cola_job() -- el
        job_lock real debe garantizar que sólo una borre los datos. Se ralentiza
        el borrado real (envolviendo purgar_registros_antiguos con un delay) para
        ensanchar la ventana de la carrera sin dejar de ejercitar el borrado
        real de Redis.
        """
        await self._crear_item_en_estado(1, "SC-OLD-OK", SmartStatus.COMPLETED.value, dias_antiguedad=40)

        original_purgar = QueueService.purgar_registros_antiguos
        llamadas = []

        async def _purgar_lento(self_qs, *args, **kwargs):
            llamadas.append(1)
            await asyncio.sleep(0.2)
            return await original_purgar(self_qs, *args, **kwargs)

        with patch.object(settings, "QUEUE_RETENTION_DAYS", 30), \
             patch.object(settings, "QUEUE_RETENTION_DAYS_DLQ", 90), \
             patch.object(QueueService, "purgar_registros_antiguos", _purgar_lento):
            await asyncio.gather(purgar_cola_job(), purgar_cola_job())

        self.assertEqual(len(llamadas), 1, "El job_lock debe impedir que dos corridas concurrentes purguen a la vez.")
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:item:1"))
        self.assertIsNone(await self.redis.get(self.lock_key))


if __name__ == "__main__":
    unittest.main()
