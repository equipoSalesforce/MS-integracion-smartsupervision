# tests/test_idempotency_registro_y_checkpoint.py
"""
Cobertura de IdempotencyService.registrar_exito (rama de reintento/backoff),
liberar_por_fallo_definitivo/liberar_operacion_por_error, y el checkpoint
durable por archivo (obtener_archivos_completados/marcar_archivo_completado/
limpiar_checkpoint_archivos) usado por S3StorageService.transferir_lote_s3_a_sfc
para no reenviar adjuntos ya confirmados. Antes sin cobertura directa.

registrar_exito se prueba con Redis mockeado (determinista: falla N veces y
luego responde, sin depender de una caída real). El checkpoint se prueba
contra Redis real (son operaciones HASH simples, mismo criterio que el resto
de tests de integración de la cola/idempotencia).
"""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.idempotency_service import IdempotencyService

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


class TestRegistrarExitoConReintento(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.payload = {"Smart_Code__c": "SC-1", "Status": "Closed"}

    async def test_reintenta_y_tiene_exito_en_el_segundo_intento(self):
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(side_effect=[ConnectionError("redis caido"), None])
        idem = IdempotencyService(mock_redis)

        with patch("app.services.idempotency_service.asyncio.sleep", new=AsyncMock()):
            await idem.registrar_exito(smart_code="SC-1", payload_dict=self.payload, sfc_response={"ok": True})

        self.assertEqual(mock_redis.set.await_count, 2)

    async def test_propaga_tras_agotar_reintentos(self):
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(side_effect=ConnectionError("redis caido de forma sostenida"))
        idem = IdempotencyService(mock_redis)

        with patch("app.services.idempotency_service.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError) as ctx:
                await idem.registrar_exito(
                    smart_code="SC-1", payload_dict=self.payload, sfc_response={"ok": True},
                    max_intentos_persistencia=3
                )

        self.assertEqual(mock_redis.set.await_count, 3)
        self.assertIn("SC-1", str(ctx.exception))

    async def test_sin_redis_lanza_de_inmediato(self):
        idem = IdempotencyService(redis_client=None)
        with self.assertRaises(RuntimeError):
            await idem.registrar_exito(smart_code="SC-1", payload_dict=self.payload, sfc_response={})


class TestLiberarPorFalloYPorError(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.payload = {"Smart_Code__c": "SC-1", "Status": "Closed"}

    async def test_liberar_por_fallo_definitivo_borra_la_clave(self):
        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock()
        idem = IdempotencyService(mock_redis)

        await idem.liberar_por_fallo_definitivo(smart_code="SC-1", payload_dict=self.payload)

        mock_redis.delete.assert_awaited_once()

    async def test_liberar_por_fallo_definitivo_sin_redis_no_hace_nada(self):
        idem = IdempotencyService(redis_client=None)
        await idem.liberar_por_fallo_definitivo(smart_code="SC-1", payload_dict=self.payload)  # No debe lanzar.

    async def test_liberar_por_fallo_definitivo_error_de_redis_no_propaga(self):
        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock(side_effect=ConnectionError("caido"))
        idem = IdempotencyService(mock_redis)

        await idem.liberar_por_fallo_definitivo(smart_code="SC-1", payload_dict=self.payload)  # No debe lanzar.

    async def test_liberar_operacion_por_error_borra_la_clave(self):
        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock()
        idem = IdempotencyService(mock_redis)

        await idem.liberar_operacion_por_error(smart_code="SC-1", payload_dict=self.payload)

        mock_redis.delete.assert_awaited_once()

    async def test_liberar_operacion_por_error_sin_redis_no_hace_nada(self):
        idem = IdempotencyService(redis_client=None)
        await idem.liberar_operacion_por_error(smart_code="SC-1", payload_dict=self.payload)  # No debe lanzar.


@unittest.skipUnless(
    _REDIS_OK,
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de integración de checkpoint. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarlas."
)
class TestCheckpointArchivos(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.idem = IdempotencyService(self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_sin_archivos_marcados_retorna_vacio(self):
        self.assertEqual(await self.idem.obtener_archivos_completados("SC-1"), set())

    async def test_marcar_y_obtener_archivo_completado(self):
        await self.idem.marcar_archivo_completado("SC-1", "quejas/SC-1/doc.pdf", metadata={"file_name": "doc.pdf"})

        completados = await self.idem.obtener_archivos_completados("SC-1")

        self.assertEqual(completados, {"quejas/SC-1/doc.pdf"})

    async def test_marcar_varios_archivos_se_acumulan(self):
        await self.idem.marcar_archivo_completado("SC-1", "a.pdf")
        await self.idem.marcar_archivo_completado("SC-1", "b.pdf")

        completados = await self.idem.obtener_archivos_completados("SC-1")

        self.assertEqual(completados, {"a.pdf", "b.pdf"})

    async def test_limpiar_checkpoint_borra_todos_los_archivos_del_caso(self):
        await self.idem.marcar_archivo_completado("SC-1", "a.pdf")

        await self.idem.limpiar_checkpoint_archivos("SC-1")

        self.assertEqual(await self.idem.obtener_archivos_completados("SC-1"), set())

    async def test_checkpoints_de_casos_distintos_no_se_mezclan(self):
        await self.idem.marcar_archivo_completado("SC-1", "a.pdf")
        await self.idem.marcar_archivo_completado("SC-2", "b.pdf")

        self.assertEqual(await self.idem.obtener_archivos_completados("SC-1"), {"a.pdf"})
        self.assertEqual(await self.idem.obtener_archivos_completados("SC-2"), {"b.pdf"})


class TestCheckpointArchivosSinRedisOFallando(unittest.IsolatedAsyncioTestCase):

    async def test_obtener_sin_redis_retorna_vacio(self):
        idem = IdempotencyService(redis_client=None)
        self.assertEqual(await idem.obtener_archivos_completados("SC-1"), set())

    async def test_marcar_sin_redis_no_lanza(self):
        idem = IdempotencyService(redis_client=None)
        await idem.marcar_archivo_completado("SC-1", "a.pdf")  # No debe lanzar.

    async def test_limpiar_sin_redis_no_lanza(self):
        idem = IdempotencyService(redis_client=None)
        await idem.limpiar_checkpoint_archivos("SC-1")  # No debe lanzar.

    async def test_obtener_con_error_de_redis_falla_abierto_retorna_vacio(self):
        mock_redis = MagicMock()
        mock_redis.hkeys = AsyncMock(side_effect=ConnectionError("caido"))
        idem = IdempotencyService(mock_redis)

        self.assertEqual(await idem.obtener_archivos_completados("SC-1"), set())

    async def test_marcar_con_error_de_redis_no_propaga(self):
        mock_redis = MagicMock()
        mock_redis.hset = AsyncMock(side_effect=ConnectionError("caido"))
        idem = IdempotencyService(mock_redis)

        await idem.marcar_archivo_completado("SC-1", "a.pdf")  # No debe lanzar.


if __name__ == "__main__":
    unittest.main()
