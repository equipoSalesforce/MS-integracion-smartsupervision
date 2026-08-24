# tests/test_queue_overwrite_resets_sfc_completado.py
"""
Regresión de dos hallazgos de code review (2026-08-24) en la misma rama de
sobrescritura de ENQUEUE_LUA_SCRIPT (encolar_despacho sobre un smart_code que
ya tenía un item PENDIENTE): reseteaba 'payload_json'/'estado'/'version'/
'ultimo_error', pero dejaba 'sfc_completado'/'sfc_response'/'intentos'
intactos, heredados del contenido ANTERIOR.

1. sfc_completado/sfc_response: si el contenido anterior ya había sido
   despachado con éxito (SFC_DONE -- que sigue en el set PENDIENTE porque aún
   falta el webhook al CRM, ver SmartStatus.SFC_DONE) y llegaba un evento
   nuevo del mismo smart_code antes de que ese webhook terminara, el item
   sobrescrito heredaba sfc_completado=true. El scheduler
   (_ejecutar_paso_sfc en scheduler.py) lee ese flag y se salta por completo
   el envío real del contenido nuevo a la SFC, reutilizando la respuesta
   vieja como si el contenido nuevo ya hubiera sido transmitido -- pérdida
   silenciosa de datos que contradecía la garantía documentada en
   FLUJO_MOMENTOS.md.

2. intentos: heredar los reintentos fallidos del contenido ANTERIOR podía
   agotar max_intentos y mandar el contenido NUEVO a FAILED_FINAL/DLQ en su
   primer fallo real, sin haberle dado sus propios reintentos.

Se prueba contra Redis real: el bug vivía en los scripts Lua, no en el
wrapper de Python, así que un mock del cliente Redis no lo habría detectado.
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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo la regresión de sobrescritura de sfc_completado. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarla."
)
class TestSobrescrituraReseteaSfcCompletado(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_evento_nuevo_tras_sfc_done_resetea_sfc_completado_y_response(self):
        # 1. Contenido ORIGINAL encolado y despachado con éxito a la SFC (SFC_DONE),
        #    aún esperando el webhook al CRM -- por eso sigue en el set PENDIENTE.
        item_v1 = await self.queue_service.encolar_despacho(
            smart_code="SC-BUG-1", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-BUG-1", "Description": "contenido ORIGINAL"},
            error_inicial="timeout inicial"
        )
        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item_v1.id, worker_id="worker-1", lease_segundos=60
        )
        resultado_sfc_done = await self.queue_service.marcar_sfc_completado(
            reclamado.id, worker_id="worker-1", expected_version=reclamado.version,
            smart_code="SC-BUG-1", payload_dict=reclamado.payload_json,
            sfc_response={"status": "success", "message": "SFC recibió el contenido ORIGINAL"}
        )
        self.assertEqual(resultado_sfc_done, "completed")

        # Confirma la premisa: SFC_DONE no saca el item del set PENDIENTE (sigue
        # esperando el paso 2 -- notificar al CRM).
        sigue_pendiente = await self.redis.sismember("{sfc:queue}:status:PENDIENTE", str(item_v1.id))
        self.assertTrue(sigue_pendiente)

        # 2. Llega un evento NUEVO del mismo smart_code antes de que el webhook al
        #    CRM termine -- debe sobrescribir el mismo registro (mismo id).
        item_v2 = await self.queue_service.encolar_despacho(
            smart_code="SC-BUG-1", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-BUG-1", "Description": "contenido NUEVO"},
            error_inicial="nuevo evento"
        )

        self.assertEqual(item_v2.id, item_v1.id)
        self.assertTrue(item_v2.es_duplicado)
        self.assertEqual(item_v2.version, 2)
        self.assertEqual(item_v2.payload_json["Description"], "contenido NUEVO")

        # 3. El fix: sfc_completado/sfc_response deben resetearse -- el contenido
        #    nuevo todavía NO ha sido enviado a la SFC, sin importar que el
        #    contenido anterior sí lo haya sido.
        self.assertFalse(item_v2.sfc_completado)
        self.assertIsNone(item_v2.sfc_response)

    async def test_evento_nuevo_tras_fallos_previos_resetea_intentos(self):
        from unittest.mock import patch

        from app.core.config import settings

        with patch.object(settings, "QUEUE_MAX_RETRIES", 5):
            item_v1 = await self.queue_service.encolar_despacho(
                smart_code="SC-BUG-2", tipo_operacion="AUTO",
                payload_json={"Smart_Code__c": "SC-BUG-2", "Description": "contenido ORIGINAL"},
                error_inicial="timeout inicial"
            )

            # Simula varios fallos reales del contenido ORIGINAL (sin llegar al DLQ).
            for _ in range(3):
                reclamado = await self.queue_service.reclamar_item_para_procesamiento(
                    registro_id=item_v1.id, worker_id="worker-1", lease_segundos=60
                )
                resultado_fallo = await self.queue_service.registrar_fallo(
                    item=reclamado, error_msg="fallo simulado", worker_id="worker-1"
                )
                self.assertEqual(resultado_fallo, "failed")

            # encolar_despacho ya arranca en intentos=1; cada fallo suma uno más.
            registros = await self.queue_service.obtener_todos_los_encolados()
            self.assertEqual(registros[0].intentos, 4)

            # Llega un evento NUEVO del mismo smart_code -- no debe heredar los 3
            # intentos fallidos de un contenido que nunca se le dio la oportunidad
            # de intentar.
            item_v2 = await self.queue_service.encolar_despacho(
                smart_code="SC-BUG-2", tipo_operacion="AUTO",
                payload_json={"Smart_Code__c": "SC-BUG-2", "Description": "contenido NUEVO"},
                error_inicial="nuevo evento"
            )

        self.assertEqual(item_v2.id, item_v1.id)
        self.assertTrue(item_v2.es_duplicado)
        self.assertEqual(item_v2.intentos, 1)


if __name__ == "__main__":
    unittest.main()
