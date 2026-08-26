# tests/test_queue_encolar_respeta_categoria_de_operacion.py
"""
Regresión de un hallazgo de revisión (2026-08-26): el fix N1
(cancelar_pendiente_por_smart_code) hizo que la CANCELACIÓN de un item
pendiente sea consciente de la categoría de operación -- pero encolar_despacho
nunca recibió el mismo tratamiento. ENQUEUE_LUA_SCRIPT sobrescribía el
payload_json de un item pendiente incondicionalmente, sin mirar si el evento
entrante era la MISMA obligación regulatoria (mismo tipo de operación) que el
contenido ya encolado.

Reproducido: un reporte de FRAUDE encolado (con tipo_fraude__c y evidencia en
archivos_s3) quedaba completamente reemplazado y perdido cuando un TRÁMITE del
mismo smart_code también fallaba y se encolaba después -- fraude y trámite son
obligaciones regulatorias separadas (mismo principio que N1), no un simple
"la última versión gana" como sí lo es una segunda actualización de la MISMA
categoría.

Corregido: ENQUEUE_LUA_SCRIPT ahora guarda `operacion` (calculada en Python vía
IdempotencyService.infer_operation_type, igual que cancelar_pendiente_por_
smart_code) junto con cada item, y rechaza la sobrescritura -- devolviendo un
conflicto en vez de pisar el contenido -- si la operación entrante difiere de
la ya encolada. encolar_despacho traduce ese conflicto a un
SfcIntegrationException(status_code=409, error_type="QUEUE_OPERATION_CONFLICT").

Se prueba contra Redis real: la protección vive en el script Lua (atómica),
no en un chequeo de Python separado que podría tener una ventana de carrera.
"""
import os
import json
import unittest

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, QUEUE_PREFIX
from app.core.exceptions import SfcIntegrationException
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


@unittest.skipUnless(
    _REDIS_OK,
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo la regresión de categoría de "
    "operación al encolar. Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarla."
)
class TestEncolarRespetaCategoriaDeOperacion(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

        self.payload_fraude = {
            "Smart_Code__c": "SC-FRAUDE-1",
            "Status": "In Progress",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Suplantacion",
            "archivos_s3": [{"nombre_archivo": "inv_fraude.pdf", "s3_key": "caso/SC-FRAUDE-1/inv_fraude.pdf", "bucket": "b"}],
        }
        self.payload_tramite = {
            "Smart_Code__c": "SC-FRAUDE-1",
            "Status": "In Progress",
            "archivos_s3": [],
        }

    async def test_operacion_distinta_no_sobrescribe_y_lanza_conflicto(self):
        await self.queue_service.encolar_despacho(
            smart_code="SC-FRAUDE-1", tipo_operacion="AUTO",
            payload_json=self.payload_fraude, error_inicial="sfc caida"
        )

        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.queue_service.encolar_despacho(
                smart_code="SC-FRAUDE-1", tipo_operacion="AUTO",
                payload_json=self.payload_tramite, error_inicial="sfc caida de nuevo"
            )

        exc = ctx.exception
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.error_type, "QUEUE_OPERATION_CONFLICT")

    async def test_operacion_distinta_preserva_el_contenido_de_fraude(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-FRAUDE-1", tipo_operacion="AUTO",
            payload_json=self.payload_fraude, error_inicial="sfc caida"
        )

        with self.assertRaises(SfcIntegrationException):
            await self.queue_service.encolar_despacho(
                smart_code="SC-FRAUDE-1", tipo_operacion="AUTO",
                payload_json=self.payload_tramite, error_inicial="sfc caida de nuevo"
            )

        raw = await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}")
        payload_guardado = json.loads(json.loads(raw)["payload_json"])
        self.assertEqual(payload_guardado.get("tipo_fraude__c"), "Externo")
        self.assertEqual(len(payload_guardado.get("archivos_s3")), 1)

    async def test_misma_operacion_si_sobrescribe_normalmente(self):
        """Control: dos eventos de la MISMA categoría (dos trámites seguidos) deben
        seguir compartiendo el mismo slot de cola, como antes del fix."""
        p1 = {"Smart_Code__c": "SC-TRAMITE-1", "Status": "In Progress", "archivos_s3": []}
        p2 = {"Smart_Code__c": "SC-TRAMITE-1", "Status": "In Progress", "archivos_s3": [], "sc_genero__c": "Femenino"}

        item1 = await self.queue_service.encolar_despacho(
            smart_code="SC-TRAMITE-1", tipo_operacion="AUTO", payload_json=p1, error_inicial="e1"
        )
        item2 = await self.queue_service.encolar_despacho(
            smart_code="SC-TRAMITE-1", tipo_operacion="AUTO", payload_json=p2, error_inicial="e2"
        )

        self.assertTrue(item2.es_duplicado)
        self.assertEqual(item2.id, item1.id)

    async def test_item_legacy_sin_campo_operacion_permite_sobrescritura(self):
        """Compatibilidad hacia atrás: un item encolado ANTES de este fix no tiene el
        campo 'operacion' -- no debe rechazarse por la ausencia del campo, sólo cuando
        el campo SÍ está presente y difiere."""
        item_id = await self.redis.incr(f"{QUEUE_PREFIX}:counter")
        legacy_data = {
            "id": item_id, "smart_code": "SC-LEGACY-1", "tipo_operacion": "AUTO",
            "payload_json": json.dumps(self.payload_fraude), "payload_hash": "x",
            "estado": "PENDIENTE", "sfc_completado": False, "sfc_response": None,
            "intentos": 1, "max_intentos": 10, "ultimo_error": "e",
            "proximo_reintento_at": "2026-01-01T00:00:00", "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00", "correlation_id": "N/A",
            "es_duplicado": False, "version": 1
            # Sin 'operacion' a propósito.
        }
        await self.redis.set(f"{QUEUE_PREFIX}:item:{item_id}", json.dumps(legacy_data))
        await self.redis.sadd(f"{QUEUE_PREFIX}:status:PENDIENTE", item_id)
        await self.redis.zadd(f"{QUEUE_PREFIX}:pending_zset", {str(item_id): 123456})
        await self.redis.set(f"{QUEUE_PREFIX}:index:SC-LEGACY-1", item_id)

        tramite = {"Smart_Code__c": "SC-LEGACY-1", "Status": "In Progress", "archivos_s3": []}
        item_nuevo = await self.queue_service.encolar_despacho(
            smart_code="SC-LEGACY-1", tipo_operacion="AUTO", payload_json=tramite, error_inicial="e-nuevo"
        )

        self.assertTrue(item_nuevo.es_duplicado)
        self.assertEqual(item_nuevo.id, item_id)

    async def test_operacion_inferida_persiste_para_la_proxima_comparacion(self):
        """Tras una sobrescritura exitosa (misma categoría), el campo 'operacion'
        guardado debe seguir reflejando la categoría vigente -- para que una TERCERA
        llegada de categoría distinta también se detecte correctamente."""
        p1 = {"Smart_Code__c": "SC-C", "Status": "In Progress", "archivos_s3": []}
        p2 = {"Smart_Code__c": "SC-C", "Status": "In Progress", "archivos_s3": [], "sc_genero__c": "Masculino"}

        await self.queue_service.encolar_despacho(
            smart_code="SC-C", tipo_operacion="AUTO", payload_json=p1, error_inicial="e1"
        )
        await self.queue_service.encolar_despacho(
            smart_code="SC-C", tipo_operacion="AUTO", payload_json=p2, error_inicial="e2"
        )

        fraude_sc_c = {**self.payload_fraude, "Smart_Code__c": "SC-C"}
        with self.assertRaises(SfcIntegrationException):
            await self.queue_service.encolar_despacho(
                smart_code="SC-C", tipo_operacion="AUTO", payload_json=fraude_sc_c, error_inicial="e3"
            )


if __name__ == "__main__":
    unittest.main()
