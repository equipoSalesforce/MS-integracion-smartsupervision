# tests/test_queue_race_protection.py
"""
Prueba de integración contra Redis REAL (no mocks) de las propiedades más
críticas exigidas por la auditoría técnica del 13/08/2026, sección 8:

  - Item 2/3: un evento B del mismo smart_code que sobrescribe a A mientras A
    está en vuelo NO puede quedar marcado COMPLETED por la finalización de A
    (protección por versión, P0-04).
  - Item 4: un worker que pierde su lease (y cuyo item fue reclamado por otro
    worker) no puede completar el item ni robarle el claim al nuevo dueño
    (protección por ownership, P0-05).

Se ejecuta contra Redis real (no un mock hecho a mano) deliberadamente: un
mock de los scripts Lua puede quedar desactualizado respecto del script real
sin que ningún test lo note — que fue exactamente lo que le pasó al fixture
`MockAsyncRedis` que existía antes en tests/test_cola_redis.py (emulaba una
versión de ENQUEUE/MARK_SUCCESS/CLAIM_ITEM anterior a P0-04/P0-05, sin campo
`version` ni verificación de ownership, y no lo usaba ningún test). Se
eliminó ese fixture y se reemplazó por esta prueba contra el motor real.

Requiere una instancia de Redis alcanzable en TEST_REDIS_URL (por defecto
redis://localhost:6379/15 — DB 15 para no chocar con un Redis de desarrollo
local en DB 0). Si Redis no está disponible, la clase completa se omite en
vez de fallar la suite.
"""
import os
import unittest

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de integración de cola. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarlas."
)
class TestQueueRaceProtection(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_overwrite_en_vuelo_no_marca_completed_el_evento_nuevo(self):
        """
        Auditoría 2026-08-13, item 2/3 y escenario 4.1: Evento A en proceso +
        llega B del mismo smart_code -> B no puede quedar COMPLETED por la
        finalización de A. El worker que procesó A debe recibir
        'version_mismatch' al intentar completar, y el item debe seguir
        contando como pendiente (con el contenido de B, no perdido).
        """
        item_a = await self.queue_service.encolar_despacho(
            smart_code="SC-100", tipo_operacion="AUTO",
            payload_json={"evento": "A"}, error_inicial="timeout inicial"
        )
        self.assertEqual(item_a.version, 1)

        worker_1 = "worker_1"
        item_reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item_a.id, worker_id=worker_1, lease_segundos=60
        )
        self.assertIsNotNone(item_reclamado)
        self.assertEqual(item_reclamado.version, 1)

        # Mientras A está "en vuelo" (reclamado por worker_1), llega B para el
        # MISMO smart_code: debe reutilizar el mismo item_id y subir la versión.
        item_b = await self.queue_service.encolar_despacho(
            smart_code="SC-100", tipo_operacion="AUTO",
            payload_json={"evento": "B"}, error_inicial="timeout B"
        )
        self.assertEqual(item_b.id, item_a.id, "Debe reutilizar el mismo item_id por colisión de smart_code")
        self.assertEqual(item_b.version, 2)
        self.assertTrue(item_b.es_duplicado)

        # worker_1 termina de procesar A (con la versión que reclamó) e
        # intenta completar: NO debe marcarse COMPLETED.
        resultado = await self.queue_service.marcar_exitoso(
            item_a.id, worker_id=worker_1, expected_version=item_reclamado.version
        )
        self.assertEqual(resultado, "version_mismatch")

        # El item debe seguir contando como pendiente (B no se pierde).
        queue_depth = await self.queue_service.contar_pendientes()
        self.assertEqual(queue_depth, 1)

    async def test_worker_sin_ownership_no_puede_completar_ni_robar_claim(self):
        """
        Auditoría 2026-08-13, item 4: un worker que pierde su lease (y cuyo
        item ya fue reclamado por otro worker) no puede completar el item
        (recibe 'not_owner'), y el nuevo dueño legítimo sí puede hacerlo.
        """
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-200", tipo_operacion="AUTO",
            payload_json={"evento": "C"}, error_inicial="timeout C"
        )

        worker_viejo = "worker_viejo"
        claim_viejo = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_viejo, lease_segundos=60
        )
        self.assertIsNotNone(claim_viejo)

        # Simula la pérdida del lease del worker viejo (expiración real o
        # liberación) borrando directamente su candado de claim en Redis, y
        # que otro worker lo reclama.
        await self.redis.delete(f"{{sfc:queue}}:claim:{item.id}")

        worker_nuevo = "worker_nuevo"
        claim_nuevo = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_nuevo, lease_segundos=60
        )
        self.assertIsNotNone(claim_nuevo, "El worker nuevo debe poder reclamar tras liberarse el lease viejo")

        # El worker viejo, ya sin ownership, intenta completar: debe rechazarse.
        resultado_viejo = await self.queue_service.marcar_exitoso(
            item.id, worker_id=worker_viejo, expected_version=claim_viejo.version
        )
        self.assertEqual(resultado_viejo, "not_owner")

        # El worker nuevo (dueño legítimo) sí puede completarlo.
        resultado_nuevo = await self.queue_service.marcar_exitoso(
            item.id, worker_id=worker_nuevo, expected_version=claim_nuevo.version
        )
        self.assertEqual(resultado_nuevo, "completed")

    async def test_claim_worker_viejo_no_borra_el_claim_del_nuevo_dueno(self):
        """
        Complemento del anterior: el intento de completar del worker viejo NO
        debe borrar el candado activo del worker nuevo (antes del fix, MARK_SUCCESS
        borraba el claim incondicionalmente).
        """
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-300", tipo_operacion="AUTO",
            payload_json={"evento": "D"}, error_inicial="timeout D"
        )
        worker_viejo = "worker_viejo"
        claim_viejo = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_viejo, lease_segundos=60
        )
        await self.redis.delete(f"{{sfc:queue}}:claim:{item.id}")

        worker_nuevo = "worker_nuevo"
        await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_nuevo, lease_segundos=60
        )

        await self.queue_service.marcar_exitoso(
            item.id, worker_id=worker_viejo, expected_version=claim_viejo.version
        )

        # El claim del worker nuevo debe seguir vigente e intacto tras el intento fallido del viejo.
        claim_actual = await self.redis.get(f"{{sfc:queue}}:claim:{item.id}")
        self.assertEqual(claim_actual, worker_nuevo)

    async def test_happy_path_sin_carrera_completa_normalmente(self):
        """Caso base sin concurrencia: un solo worker debe poder completar sin fricción."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-400", tipo_operacion="AUTO",
            payload_json={"evento": "E"}, error_inicial="timeout E"
        )
        worker_id = "worker_unico"
        claim = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_id, lease_segundos=60
        )
        resultado = await self.queue_service.marcar_exitoso(
            item.id, worker_id=worker_id, expected_version=claim.version
        )
        self.assertEqual(resultado, "completed")
        self.assertEqual(await self.queue_service.contar_pendientes(), 0)


if __name__ == "__main__":
    unittest.main()
