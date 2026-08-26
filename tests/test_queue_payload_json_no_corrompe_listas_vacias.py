# tests/test_queue_payload_json_no_corrompe_listas_vacias.py
"""
Regresión de un hallazgo de revisión externa (N4, v5, 2026-08-25): Lua's cjson no
distingue una lista vacía de un objeto vacío al re-serializar -- una tabla vacía
"{}" siempre se codifica como objeto JSON, nunca como "[]". Antes de este fix,
ENQUEUE_LUA_SCRIPT y MARK_SFC_DONE_LUA_SCRIPT decodificaban payload_json/
sfc_response a tabla Lua antes de guardarlos dentro del item -- cualquier campo
tipo lista que arrancara vacío (ej. archivos_s3: [], la inmensa mayoría de las
quejas sin adjuntos) quedaba corrompido a {} en el contenido GUARDADO, no sólo
en un hash recalculado después.

El fix: payload_json/sfc_response se guardan como string JSON opaco (el que ya
serializa Python correctamente) en vez de decodificarse a tabla -- Lua nunca
vuelve a leer su contenido, sólo lo reemplaza.

Se prueba contra Redis real: el bug vivía en los scripts Lua, no en el wrapper
de Python -- un mock no lo habría detectado.
"""
import json
import os
import unittest

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, ColaItemRedis, QUEUE_PREFIX

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo la regresión de N4. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarla."
)
class TestPayloadJsonNoCorrompeListasVacias(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_reproduccion_directa_cjson_ya_no_corrompe_lista_vacia(self):
        """Confirma el mecanismo de fondo: guardar el JSON como string opaco (en vez
        de decodificarlo a tabla) sobrevive el round-trip de cjson sin corromperse."""
        opaco = await self.redis.eval(
            'return cjson.encode({payload_json = ARGV[1]})', 0, '{"archivos_s3":[]}'
        )
        self.assertEqual(json.loads(json.loads(opaco)["payload_json"]), {"archivos_s3": []})

        # Contraprueba: decodificarlo a tabla (el comportamiento ANTERIOR al fix)
        # sigue corrompiendo -- confirma que el bug era real y el fix es necesario.
        decodificado = await self.redis.eval(
            'return cjson.encode({payload_json = cjson.decode(ARGV[1])})', 0, '{"archivos_s3":[]}'
        )
        self.assertEqual(json.loads(decodificado)["payload_json"], {"archivos_s3": {}})

    async def test_encolar_despacho_item_nuevo_preserva_lista_vacia(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-N4-1", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-N4-1", "archivos_s3": []},
            error_inicial="SFC caída"
        )

        self.assertEqual(item.payload_json["archivos_s3"], [])

        raw_item = await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}")
        data = json.loads(raw_item)
        self.assertIsInstance(data["payload_json"], str, "payload_json debe guardarse como string JSON opaco")
        self.assertEqual(json.loads(data["payload_json"])["archivos_s3"], [])

    async def test_encolar_despacho_sobrescritura_preserva_lista_vacia(self):
        """Cubre la OTRA rama de ENQUEUE_LUA_SCRIPT (item ya pendiente, sobrescrito
        por un evento nuevo del mismo smart_code) -- tiene su propio cjson.decode."""
        await self.queue_service.encolar_despacho(
            smart_code="SC-N4-2", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-N4-2", "Description": "primer intento"},
            error_inicial="SFC caída"
        )

        item_v2 = await self.queue_service.encolar_despacho(
            smart_code="SC-N4-2", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-N4-2", "archivos_s3": [], "Description": "segundo intento"},
            error_inicial="SFC caída de nuevo"
        )

        self.assertTrue(item_v2.es_duplicado)
        self.assertEqual(item_v2.payload_json["archivos_s3"], [])

    async def test_marcar_sfc_completado_preserva_lista_vacia_en_sfc_response(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-N4-3", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-N4-3"}, error_inicial="SFC caída"
        )
        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        await self.queue_service.marcar_sfc_completado(
            reclamado.id, worker_id="worker_1", expected_version=reclamado.version,
            smart_code="SC-N4-3", payload_dict=reclamado.payload_json,
            sfc_response={"status": "success", "advertencias": []}
        )

        raw_item = await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}")
        data = json.loads(raw_item)
        self.assertIsInstance(data["sfc_response"], str, "sfc_response debe guardarse como string JSON opaco")
        self.assertEqual(json.loads(data["sfc_response"])["advertencias"], [])

    async def test_reclamar_item_devuelve_payload_json_ya_decodificado(self):
        """El caller (scheduler.py) siempre debe ver item.payload_json como un dict
        normal -- la representación opaca es un detalle interno de almacenamiento."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-N4-4", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-N4-4", "archivos_s3": []}, error_inicial="SFC caída"
        )

        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        self.assertIsInstance(reclamado.payload_json, dict)
        self.assertEqual(reclamado.payload_json["archivos_s3"], [])


class TestColaItemRedisCompatibilidadHaciaAtras(unittest.TestCase):
    """ColaItemRedis debe seguir aceptando payload_json/sfc_response ya decodificados
    (dict) -- items encolados antes de este fix que sigan en Redis durante un
    despliegue en curso, o construcciones directas en Python/tests."""

    def test_acepta_payload_json_como_dict_legado(self):
        item = ColaItemRedis({"id": 1, "smart_code": "SC-X", "payload_json": {"archivos_s3": []}})
        self.assertEqual(item.payload_json, {"archivos_s3": []})

    def test_acepta_payload_json_como_string_nuevo_formato(self):
        item = ColaItemRedis({"id": 1, "smart_code": "SC-X", "payload_json": '{"archivos_s3": []}'})
        self.assertEqual(item.payload_json, {"archivos_s3": []})

    def test_payload_json_ausente_no_lanza(self):
        item = ColaItemRedis({"id": 1, "smart_code": "SC-X"})
        self.assertEqual(item.payload_json, {})

    def test_sfc_response_ausente_es_none(self):
        item = ColaItemRedis({"id": 1, "smart_code": "SC-X"})
        self.assertIsNone(item.sfc_response)

    def test_sfc_response_como_string_se_decodifica(self):
        item = ColaItemRedis({"id": 1, "smart_code": "SC-X", "sfc_response": '{"status": "success"}'})
        self.assertEqual(item.sfc_response, {"status": "success"})


if __name__ == "__main__":
    unittest.main()
