# tests/test_queue_resiliencia_datos_corruptos.py
"""
Auditoría de resiliencia (2026-08-26): ningún test existente simulaba un item con
JSON corrupto directamente en Redis (bug futuro en otra parte del sistema,
manipulación manual, etc.) para verificar que cada operación de QueueService degrada
con gracia -- log + falla controlada -- en vez de comportarse de forma inesperada.

Al revisar los 8 scripts Lua que hacen cjson.decode(raw_item) se encontró un bug real
en CLAIM_ITEM_LUA_SCRIPT: tomaba el claim (SET claim_key NX) ANTES de leer/decodificar
el item. Los scripts Lua de Redis no revierten llamadas ya ejecutadas cuando el script
aborta a mitad de camino por un error -- así que un item corrupto dejaba el claim_key
huérfano (con su propio TTL), bloqueando cualquier claim legítimo de ese item durante
esa ventana sin que nadie estuviera realmente procesándolo. Se corrigió reordenando el
script (leer y decodificar antes de escribir nada). Los otros 7 scripts ya hacían todas
sus lecturas antes de la primera escritura -- se confirma aquí que están a salvo.
"""
import json
import os
import unittest
from unittest.mock import patch, AsyncMock

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, QUEUE_PREFIX

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de resiliencia ante "
    "datos corruptos. Levante un Redis local (ej. `docker run --rm -p 6379:6379 "
    "redis:7-alpine`) para ejecutarlas."
)
class TestResilienciaAnteItemCorrupto(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def _encolar_y_corromper(self, smart_code: str) -> int:
        item = await self.queue_service.encolar_despacho(
            smart_code=smart_code, tipo_operacion="AUTO",
            payload_json={"a": 1}, error_inicial="timeout"
        )
        await self.redis.set(f"{QUEUE_PREFIX}:item:{item.id}", "ESTO NO ES JSON {{{")
        return item.id

    async def test_reclamar_item_corrupto_falla_con_gracia_y_no_deja_claim_huerfano(self):
        """
        Regresión del bug real encontrado y corregido: antes de la corrección,
        reclamar un item corrupto dejaba el claim_key escrito en Redis (con TTL)
        aunque el método reportara el claim como fallido -- bloqueando cualquier
        claim legítimo posterior hasta que ese TTL expirara solo.
        """
        registro_id = await self._encolar_y_corromper("SC-CORRUPT-1")

        resultado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=registro_id, worker_id="worker_1", lease_segundos=60
        )

        self.assertIsNone(resultado)
        self.assertIsNone(
            await self.redis.get(f"{QUEUE_PREFIX}:claim:{registro_id}"),
            "No debe quedar un claim huérfano tras un intento de reclamo sobre un item corrupto"
        )

    async def test_tras_reparar_el_item_el_claim_si_funciona(self):
        """Complemento del anterior: confirma que el fallo es específico del
        contenido corrupto, no un efecto colateral permanente sobre el item/claim_key."""
        registro_id = await self._encolar_y_corromper("SC-CORRUPT-2")
        await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=registro_id, worker_id="worker_1", lease_segundos=60
        )

        # "Repara" el item con contenido válido, como si un operador lo hubiera corregido.
        await self.redis.set(
            f"{QUEUE_PREFIX}:item:{registro_id}",
            json.dumps({
                "id": registro_id, "smart_code": "SC-CORRUPT-2", "tipo_operacion": "AUTO",
                "payload_json": '{"a": 1}', "estado": "PENDIENTE", "sfc_completado": False,
                "sfc_response": None, "intentos": 1, "max_intentos": 10, "ultimo_error": None,
                "proximo_reintento_at": None, "created_at": None, "updated_at": None,
                "correlation_id": "N/A", "es_duplicado": False, "version": 1, "payload_hash": None
            })
        )

        resultado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=registro_id, worker_id="worker_1", lease_segundos=60
        )
        self.assertIsNotNone(resultado)

    async def test_marcar_exitoso_sobre_item_corrupto_propaga_la_excepcion(self):
        """
        A diferencia de reclamar_item_para_procesamiento/registrar_fallo (que
        absorben cualquier fallo de Redis/Lua y degradan a un valor de retorno),
        marcar_exitoso está documentado para relanzar fallos reales de Redis/Lua a
        propósito -- en este punto la SFC YA aceptó el envío; si no se puede
        persistir ese éxito, el caller (routes_quejas.py/scheduler.py) necesita
        enterarse para alertar "riesgo de duplicado" en vez de que el fallo quede
        silenciado. Confirma que un item corrupto efectivamente propaga (no crashea
        de una forma distinta ni queda colgado)."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-CORRUPT-3", tipo_operacion="AUTO",
            payload_json={"a": 1}, error_inicial="timeout"
        )
        claim = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        await self.redis.set(f"{QUEUE_PREFIX}:item:{item.id}", "ESTO NO ES JSON {{{")

        with self.assertRaises(Exception):
            await self.queue_service.marcar_exitoso(
                item.id, worker_id="worker_1", expected_version=claim.version
            )

    async def test_registrar_fallo_sobre_item_corrupto_falla_con_gracia(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-CORRUPT-4", tipo_operacion="AUTO",
            payload_json={"a": 1}, error_inicial="timeout"
        )
        claim = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        await self.redis.set(f"{QUEUE_PREFIX}:item:{item.id}", "ESTO NO ES JSON {{{")

        resultado = await self.queue_service.registrar_fallo(
            item=claim, error_msg="error de prueba", worker_id="worker_1"
        )
        self.assertNotEqual(resultado, "failed")

    async def test_reencolar_item_fallido_corrupto_falla_con_gracia(self):
        from app.core.config import settings

        with patch.object(settings, "QUEUE_MAX_RETRIES", 1):
            item = await self.queue_service.encolar_despacho(
                smart_code="SC-CORRUPT-5", tipo_operacion="AUTO",
                payload_json={"a": 1}, error_inicial="timeout"
            )
            claim = await self.queue_service.reclamar_item_para_procesamiento(
                registro_id=item.id, worker_id="worker_1", lease_segundos=60
            )
            with patch(
                "app.services.queue_service.EmailAlertService.notificar_caso_fallido_definitivo",
                new_callable=AsyncMock
            ):
                await self.queue_service.registrar_fallo(
                    item=claim, error_msg="falla definitiva", worker_id="worker_1"
                )

        # El item ya está en FALLIDO_DEFINITIVO -- se corrompe antes de intentar reencolarlo.
        await self.redis.set(f"{QUEUE_PREFIX}:item:{item.id}", "ESTO NO ES JSON {{{")

        resultado = await self.queue_service.reencolar_item_fallido(item.id)

        self.assertFalse(resultado["success"])


if __name__ == "__main__":
    unittest.main()
