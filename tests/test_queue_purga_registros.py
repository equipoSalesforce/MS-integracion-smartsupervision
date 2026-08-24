# tests/test_queue_purga_registros.py
"""
Prueba de integración contra Redis REAL (no mocks) de QueueService.purgar_registros_antiguos
/ _purgar_estado -- el job nocturno (`purgar_cola_job` en scheduler.py) que borra
definitivamente de Redis los registros EXITOSO/FALLIDO_DEFINITIVO vencidos. Hasta ahora,
0% de cobertura de líneas pese a ser lógica de borrado de datos que corre sin supervisión
todas las noches.

Mismo criterio que test_queue_race_protection.py: contra Redis real, no un mock de los
scripts/estructura de claves que puede quedar desactualizado sin que ningún test lo note.

Requiere una instancia de Redis alcanzable en TEST_REDIS_URL (por defecto
redis://localhost:6379/15). Si Redis no está disponible, la clase completa se omite.
"""
import json
import os
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, QUEUE_PREFIX
from app.core.constants import SmartStatus

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de integración de purga. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarlas."
)
class TestPurgarRegistrosAntiguos(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def _crear_item_en_estado(self, item_id: int, smart_code: str, estado: str, dias_antiguedad: float) -> None:
        """
        Inserta directamente en Redis (sin pasar por encolar/reclamar/marcar) un item
        ya en el estado y antigüedad deseados -- más simple y explícito para probar la
        purga en aislamiento de la máquina de estados que la produce.
        """
        updated_at = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(days=dias_antiguedad)).isoformat()
        item_key = f"{QUEUE_PREFIX}:item:{item_id}"
        data = {
            "id": item_id,
            "smart_code": smart_code,
            "tipo_operacion": "AUTO",
            "payload_json": {"Smart_Code__c": smart_code},
            "estado": estado,
            "sfc_completado": True,
            "sfc_response": None,
            "intentos": 1,
            "max_intentos": 10,
            "ultimo_error": None,
            "proximo_reintento_at": None,
            "created_at": updated_at,
            "updated_at": updated_at,
            "correlation_id": "N/A",
            "es_duplicado": False,
            "version": 1,
        }
        await self.redis.set(item_key, json.dumps(data, ensure_ascii=False))
        await self.redis.sadd(f"{QUEUE_PREFIX}:status:{estado}", str(item_id))
        await self.redis.zadd(f"{QUEUE_PREFIX}:created_zset", {str(item_id): 0})

    async def test_purga_completados_antiguos_pero_conserva_recientes(self):
        await self._crear_item_en_estado(1, "SC-OLD", SmartStatus.COMPLETED.value, dias_antiguedad=40)
        await self._crear_item_en_estado(2, "SC-NEW", SmartStatus.COMPLETED.value, dias_antiguedad=1)

        total_purgados = await self.queue_service.purgar_registros_antiguos(
            dias_retencion=30, dias_retencion_dlq=90
        )

        self.assertEqual(total_purgados, 1)

        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:item:1"))
        self.assertFalse(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", "1"))
        self.assertIsNone(await self.redis.zscore(f"{QUEUE_PREFIX}:created_zset", "1"))

        self.assertIsNotNone(await self.redis.get(f"{QUEUE_PREFIX}:item:2"))
        self.assertTrue(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", "2"))

    async def test_failed_final_usa_su_propia_ventana_de_retencion_dlq(self):
        """FAILED_FINAL (DLQ) se purga contra dias_retencion_dlq, independiente de dias_retencion."""
        await self._crear_item_en_estado(3, "SC-DLQ", SmartStatus.FAILED_FINAL.value, dias_antiguedad=10)

        # dias_retencion=5 purgaría un COMPLETED de 10 días, pero dias_retencion_dlq=30 no debe tocar este FAILED_FINAL.
        total_purgados = await self.queue_service.purgar_registros_antiguos(
            dias_retencion=5, dias_retencion_dlq=30
        )
        self.assertEqual(total_purgados, 0)
        self.assertIsNotNone(await self.redis.get(f"{QUEUE_PREFIX}:item:3"))

        # Con una ventana DLQ más corta que la antigüedad real, ahora sí se purga.
        total_purgados = await self.queue_service.purgar_registros_antiguos(
            dias_retencion=5, dias_retencion_dlq=3
        )
        self.assertEqual(total_purgados, 1)
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:item:3"))

    async def test_registro_huerfano_en_set_se_limpia_sin_contar_como_purgado(self):
        """
        Un id presente en el set de estado pero sin su item:{id} correspondiente
        (huérfano, ej. borrado a mano o por una carrera) debe removerse del set
        igualmente, pero NO debe contar en total_purgados -- ese conteo refleja
        borrados reales de datos, no limpieza de referencias huérfanas.
        """
        await self.redis.sadd(f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", "999")

        total_purgados = await self.queue_service.purgar_registros_antiguos(
            dias_retencion=30, dias_retencion_dlq=90
        )

        self.assertEqual(total_purgados, 0)
        self.assertFalse(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", "999"))

    async def test_sin_registros_no_purga_nada(self):
        total_purgados = await self.queue_service.purgar_registros_antiguos(
            dias_retencion=30, dias_retencion_dlq=90
        )
        self.assertEqual(total_purgados, 0)


class TestPurgarRegistrosAntiguosSinRedis(unittest.IsolatedAsyncioTestCase):

    async def test_sin_cliente_redis_retorna_cero_sin_lanzar(self):
        queue_service = QueueService(redis_client=None)
        total_purgados = await queue_service.purgar_registros_antiguos(dias_retencion=30, dias_retencion_dlq=90)
        self.assertEqual(total_purgados, 0)


if __name__ == "__main__":
    unittest.main()
