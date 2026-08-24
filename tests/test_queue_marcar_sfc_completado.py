# tests/test_queue_marcar_sfc_completado.py
"""
Cobertura de QueueService.marcar_sfc_completado -- la transición atómica que
persiste SFC_DONE en la cola Y el registro COMPLETED de idempotencia en el
MISMO script Lua (P0-01/P0-02), para no poder quedar en un estado dividido
si una de las dos escrituras fallara por separado. Antes sin cobertura
directa de las ramas not_owner/version_mismatch/not_found/reintento.

Los resultados de ownership/version (not_owner, version_mismatch,
item_not_found) se prueban contra Redis real -- mismo criterio que
test_queue_race_protection.py. El reintento/backoff se prueba con Redis
mockeado (determinista).
"""
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de integración de marcar_sfc_completado. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarlas."
)
class TestMarcarSfcCompletado(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_completa_exitosamente_con_ownership_valido(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-1", tipo_operacion="AUTO", payload_json={"Smart_Code__c": "SC-1"}, error_inicial="timeout"
        )
        item_reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        resultado = await self.queue_service.marcar_sfc_completado(
            item.id, worker_id="worker_1", expected_version=item_reclamado.version,
            smart_code="SC-1", payload_dict={"Smart_Code__c": "SC-1"}, sfc_response={"status": "ok"}
        )

        self.assertEqual(resultado, "completed")

    async def test_not_owner_si_otro_worker_tiene_el_claim(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-2", tipo_operacion="AUTO", payload_json={"Smart_Code__c": "SC-2"}, error_inicial="timeout"
        )
        item_reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_dueno", lease_segundos=60
        )

        resultado = await self.queue_service.marcar_sfc_completado(
            item.id, worker_id="worker_intruso", expected_version=item_reclamado.version,
            smart_code="SC-2", payload_dict={"Smart_Code__c": "SC-2"}
        )

        self.assertEqual(resultado, "not_owner")

    async def test_version_mismatch_si_el_item_fue_sobrescrito(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-3", tipo_operacion="AUTO", payload_json={"Smart_Code__c": "SC-3"}, error_inicial="timeout"
        )
        item_reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        # Un evento nuevo del mismo caso llega mientras "worker_1" procesaba la versión anterior.
        await self.queue_service.encolar_despacho(
            smart_code="SC-3", tipo_operacion="AUTO", payload_json={"Smart_Code__c": "SC-3", "v": 2}, error_inicial="timeout"
        )

        resultado = await self.queue_service.marcar_sfc_completado(
            item.id, worker_id="worker_1", expected_version=item_reclamado.version,
            smart_code="SC-3", payload_dict={"Smart_Code__c": "SC-3"}
        )

        self.assertEqual(resultado, "version_mismatch")

    async def test_registro_totalmente_inexistente_da_not_owner_no_not_found(self):
        """Documenta el comportamiento real: sin claim previo, GET claim_key da nil,
        que nunca es igual a worker_id -- el chequeo de ownership falla antes de
        siquiera comprobar si el item existe."""
        resultado = await self.queue_service.marcar_sfc_completado(
            999999, worker_id="worker_1", expected_version=1,
            smart_code="SC-INEXISTENTE", payload_dict={"Smart_Code__c": "SC-INEXISTENTE"}
        )

        self.assertEqual(resultado, "not_owner")

    async def test_item_not_found_si_el_item_desaparecio_con_el_claim_intacto(self):
        # 🟢 El script Lua valida ownership vía el claim ANTES de comprobar si el item
        # existe -- un id totalmente inexistente (sin claim previo) da "not_owner", no
        # "item_not_found" (ver MARK_SFC_DONE_LUA_SCRIPT). Para llegar genuinamente a
        # "item_not_found" hace falta un claim válido cuyo item_key desapareció por otra vía.
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-4", tipo_operacion="AUTO", payload_json={"Smart_Code__c": "SC-4"}, error_inicial="timeout"
        )
        item_reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        await self.redis.delete(f"{{sfc:queue}}:item:{item.id}")

        resultado = await self.queue_service.marcar_sfc_completado(
            item.id, worker_id="worker_1", expected_version=item_reclamado.version,
            smart_code="SC-4", payload_dict={"Smart_Code__c": "SC-4"}
        )

        self.assertEqual(resultado, "not_found")


class TestMarcarSfcCompletadoReintentoYFallos(unittest.IsolatedAsyncioTestCase):

    async def test_sin_redis_lanza_de_inmediato(self):
        queue_service = QueueService(redis_client=None)
        with self.assertRaises(RuntimeError):
            await queue_service.marcar_sfc_completado(
                1, worker_id="w", expected_version=1, smart_code="SC-1", payload_dict={"Smart_Code__c": "SC-1"}
            )

    async def test_reintenta_y_tiene_exito_en_el_segundo_intento(self):
        mock_redis = MagicMock()
        exito = '{"success": true}'
        mock_redis.eval = AsyncMock(side_effect=[ConnectionError("redis caido"), exito])
        queue_service = QueueService(redis_client=mock_redis)

        with patch("app.services.queue_service.asyncio.sleep", new=AsyncMock()):
            resultado = await queue_service.marcar_sfc_completado(
                1, worker_id="w", expected_version=1, smart_code="SC-1", payload_dict={"Smart_Code__c": "SC-1"}
            )

        self.assertEqual(resultado, "completed")
        self.assertEqual(mock_redis.eval.await_count, 2)

    async def test_propaga_tras_agotar_reintentos(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis caido de forma sostenida"))
        queue_service = QueueService(redis_client=mock_redis)

        with patch("app.services.queue_service.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError) as ctx:
                await queue_service.marcar_sfc_completado(
                    1, worker_id="w", expected_version=1, smart_code="SC-1", payload_dict={"Smart_Code__c": "SC-1"},
                    max_intentos_persistencia=3
                )

        self.assertEqual(mock_redis.eval.await_count, 3)
        self.assertIn("1", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
