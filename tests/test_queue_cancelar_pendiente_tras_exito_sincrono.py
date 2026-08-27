# tests/test_queue_cancelar_pendiente_tras_exito_sincrono.py
"""
Regresión de un hallazgo de code review (2026-08-25): un despacho SÍNCRONO
exitoso (POST /sync/despacho respondiendo 200 directamente) nunca consultaba
ni tocaba la cola de contingencia. Si un intento ANTERIOR del mismo
smart_code había fallado (SFC caída/lenta) y quedó encolado, y el CRM
reenviaba después con contenido más reciente que esta vez sí se despachaba
con éxito por la vía síncrona, el item viejo con contenido OBSOLETO seguía
pendiente en Redis -- el próximo ciclo del scheduler lo reintentaba y lo
reenviaba a la SFC, pudiendo pisar en silencio los campos que el despacho
síncrono más reciente ya había corregido.

A diferencia de los bugs ya corregidos en ENQUEUE_LUA_SCRIPT (que protegen el
caso en que AMBOS intentos pasan por la cola), este cubre el caso en que el
segundo intento tiene éxito por la vía síncrona y NUNCA pasa por la cola --
por eso el mecanismo de "version" del item de cola no alcanza a proteger
nada, ya que ese despacho síncrono nunca lo toca.

Se prueba contra Redis real: el fix vive en un script Lua nuevo
(CANCELAR_PENDIENTE_POR_SMART_CODE_LUA_SCRIPT), no en el wrapper de Python.
"""
import os
import unittest

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo la regresión de cancelación de cola tras "
    "éxito síncrono. Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarla."
)
class TestCancelarPendientePorSmartCode(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_cancela_item_pendiente_sin_reclamar(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-SYNC-1", tipo_operacion="AUTO",
            payload_json={"Description": "contenido OBSOLETO"}, error_inicial="timeout inicial"
        )

        cancelado = await self.queue_service.cancelar_pendiente_por_smart_code(
            "SC-SYNC-1", operacion_actual="M3_UPDATE"
        )

        self.assertTrue(cancelado)
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}"))
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:index:SC-SYNC-1:M3_UPDATE"))
        self.assertEqual(await self.queue_service.contar_pendientes(), 0)
        es_miembro_zset = await self.redis.zscore(f"{QUEUE_PREFIX}:pending_zset", str(item.id))
        self.assertIsNone(es_miembro_zset)
        es_miembro_created = await self.redis.zscore(f"{QUEUE_PREFIX}:created_zset", str(item.id))
        self.assertIsNone(es_miembro_created)

    async def test_cancela_item_reclamado_sin_tocar_el_claim_activo(self):
        """
        Si un worker tiene el item reclamado en el instante exacto de la cancelación
        (ventana mínima), el claim se deja intacto -- la escritura posterior del
        worker (marcar_exitoso/marcar_sfc_completado/registrar_fallo) ya maneja de
        forma segura un item_key inexistente (devuelve 'item_not_found', libera el
        claim y no corrompe nada); el claim huérfano expira solo por su propio PX.
        """
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-SYNC-2", tipo_operacion="AUTO",
            payload_json={"Description": "contenido OBSOLETO"}, error_inicial="timeout inicial"
        )
        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        self.assertIsNotNone(reclamado)

        cancelado = await self.queue_service.cancelar_pendiente_por_smart_code(
            "SC-SYNC-2", operacion_actual="M3_UPDATE"
        )

        self.assertTrue(cancelado)
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}"))
        claim_actual = await self.redis.get(f"{QUEUE_PREFIX}:claim:{item.id}")
        self.assertEqual(claim_actual, "worker_1", "El claim activo no debe tocarse")

        # La escritura posterior del worker sobre el item ya cancelado debe
        # rechazarse de forma segura, no corromper nada.
        resultado = await self.queue_service.marcar_exitoso(
            item.id, worker_id="worker_1", expected_version=reclamado.version
        )
        self.assertEqual(resultado, "not_found")

    async def test_cancelacion_durante_reintento_activo_registrar_fallo_no_corrompe_nada(self):
        """
        Pregunta de revisión (2026-08-25): ¿qué pasa si esto sucede mientras un
        worker está reintentando el envío -- ej. el intento de A vuelve a fallar en
        SFC justo después de que B (contenido corregido) tuvo éxito por la vía
        síncrona y canceló este item? El claim NO se toca, así que la validación de
        ownership de REGISTRAR_FALLO_LUA_SCRIPT sigue pasando (current_owner ==
        worker_id) -- pero el item ya no existe, así que cae en 'item_not_found':
        no incrementa intentos, no manda nada a FAILED_FINAL/DLQ, no corrompe el
        contenido de B (que ni siquiera pasó por la cola).
        """
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-SYNC-5", tipo_operacion="AUTO",
            payload_json={"Description": "contenido con dato equivocado"}, error_inicial="timeout inicial"
        )
        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        await self.queue_service.cancelar_pendiente_por_smart_code("SC-SYNC-5", operacion_actual="M3_UPDATE")

        resultado = await self.queue_service.registrar_fallo(
            item=reclamado, error_msg="SFC rechazó el dato equivocado otra vez", worker_id="worker_1"
        )
        self.assertEqual(resultado, "not_found")
        # No debe haber resucitado el item ni el índice de la operación.
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}"))
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:index:SC-SYNC-5:M3_UPDATE"))

    async def test_cancelacion_durante_reintento_activo_marcar_sfc_completado_no_corrompe_nada(self):
        """Mismo escenario, pero el intento de A que estaba en vuelo SÍ tiene éxito
        en SFC justo después de la cancelación: marcar_sfc_completado debe
        rechazarse igual de limpio ('not_found'), sin persistir sfc_completado
        sobre un item que ya no existe ni resucitarlo."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-SYNC-6", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-SYNC-6"}, error_inicial="timeout inicial"
        )
        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        await self.queue_service.cancelar_pendiente_por_smart_code("SC-SYNC-6", operacion_actual="M3_UPDATE")

        resultado = await self.queue_service.marcar_sfc_completado(
            reclamado.id, worker_id="worker_1", expected_version=reclamado.version,
            smart_code="SC-SYNC-6", payload_dict=reclamado.payload_json,
            sfc_response={"status": "success", "message": "respuesta tardía del intento viejo"}
        )
        self.assertEqual(resultado, "not_found")
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}"))

    async def test_sin_item_pendiente_no_hace_nada_y_retorna_false(self):
        cancelado = await self.queue_service.cancelar_pendiente_por_smart_code(
            "SC-SYNC-INEXISTENTE", operacion_actual="M3_UPDATE"
        )
        self.assertFalse(cancelado)

    async def test_item_ya_completado_no_se_toca_dos_veces(self):
        """Tras marcar_exitoso, MARK_SUCCESS ya borra el index_key -- una cancelación
        posterior no debe encontrar nada que tocar (no debe lanzar ni afectar nada)."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-SYNC-3", tipo_operacion="AUTO",
            payload_json={"a": 1}, error_inicial="timeout inicial"
        )
        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        await self.queue_service.marcar_exitoso(
            item.id, worker_id="worker_1", expected_version=reclamado.version
        )

        cancelado = await self.queue_service.cancelar_pendiente_por_smart_code(
            "SC-SYNC-3", operacion_actual="M3_UPDATE"
        )

        self.assertFalse(cancelado)

    async def test_tras_cancelar_un_nuevo_encolado_crea_item_nuevo_no_lo_reutiliza(self):
        item_viejo = await self.queue_service.encolar_despacho(
            smart_code="SC-SYNC-4", tipo_operacion="AUTO",
            payload_json={"Description": "contenido OBSOLETO"}, error_inicial="timeout inicial"
        )
        await self.queue_service.cancelar_pendiente_por_smart_code("SC-SYNC-4", operacion_actual="M3_UPDATE")

        item_nuevo = await self.queue_service.encolar_despacho(
            smart_code="SC-SYNC-4", tipo_operacion="AUTO",
            payload_json={"Description": "otro fallo distinto, ya sin relación con el anterior"},
            error_inicial="timeout nuevo"
        )

        self.assertNotEqual(item_nuevo.id, item_viejo.id)
        self.assertFalse(item_nuevo.es_duplicado)
        self.assertEqual(item_nuevo.version, 1)

    async def test_sin_redis_retorna_false(self):
        queue_service = QueueService(redis_client=None)
        self.assertFalse(await queue_service.cancelar_pendiente_por_smart_code("SC-X", operacion_actual="M3_UPDATE"))

    async def test_smart_code_vacio_retorna_false_sin_tocar_redis(self):
        self.assertFalse(
            await self.queue_service.cancelar_pendiente_por_smart_code("", operacion_actual="M3_UPDATE")
        )


@unittest.skipUnless(
    _REDIS_OK,
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo la regresión de N1. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarla."
)
class TestCancelarPendienteRespetaCategoriaDeOperacion(unittest.IsolatedAsyncioTestCase):
    """
    🔴 FIX (hallazgo N1, revisión externa v5, 2026-08-25): reproduce el escenario
    exacto del hallazgo -- un reporte de FRAUDE queda encolado por una caída de la
    SFC, y después un TRÁMITE del mismo caso se despacha con éxito por la vía
    síncrona. La versión original cancelaba el fraude pendiente sin mirar su
    contenido -- perdiéndolo para siempre, porque nunca llegó a transmitirse a la
    SFC (a diferencia del caso "obsoleto" que este mecanismo sí debe cubrir).

    🔴 FIX (hallazgo de revisión externa, 2026-08-26, ronda 4 -- X5/Y4): el
    mecanismo que garantiza esto cambió -- ya no lee el item pendiente e infiere
    su categoría para compararla contra `operacion_actual` (dos pasos, con una
    ventana de carrera entre ambos). Ahora `index_key` ya viene particionado por
    operación (`index:{smart_code}:{operacion_actual}`), así que sólo puede
    encontrar un item de la MISMA categoría -- una operación distinta ni
    siquiera se lee, por construcción. Los tests de esta clase no cambian sus
    aserciones porque el comportamiento observable es el mismo; sólo cambia
    (y se simplifica) el mecanismo interno.
    """

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_tramite_exitoso_no_cancela_un_fraude_pendiente_de_operacion_distinta(self):
        item_fraude = await self.queue_service.encolar_despacho(
            smart_code="SC-N1-1", tipo_operacion="AUTO",
            payload_json={
                "Smart_Code__c": "SC-N1-1",
                "tipo_fraude__c": "Suplantación",
                "modalidad_fraude__c": "Phishing"
            },
            error_inicial="SFC caída"
        )

        cancelado = await self.queue_service.cancelar_pendiente_por_smart_code(
            "SC-N1-1", operacion_actual="M3_UPDATE"
        )

        self.assertFalse(cancelado, "Un trámite exitoso no debe poder cancelar un fraude pendiente distinto.")
        self.assertIsNotNone(
            await self.redis.get(f"{QUEUE_PREFIX}:item:{item_fraude.id}"),
            "El reporte de fraude debe seguir en Redis, pendiente de transmitirse a la SFC."
        )
        self.assertEqual(await self.queue_service.contar_pendientes(), 1)

    async def test_mismo_tipo_de_operacion_si_se_cancela(self):
        """Contraprueba: dos trámites (misma categoría) -- el segundo, exitoso por
        la vía síncrona, sí debe poder cancelar el primero, que quedó obsoleto."""
        item_viejo = await self.queue_service.encolar_despacho(
            smart_code="SC-N1-2", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-N1-2", "Description": "trámite con dato desactualizado"},
            error_inicial="SFC caída"
        )

        cancelado = await self.queue_service.cancelar_pendiente_por_smart_code(
            "SC-N1-2", operacion_actual="M3_UPDATE"
        )

        self.assertTrue(cancelado)
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:item:{item_viejo.id}"))

    async def test_cierre_exitoso_no_cancela_fraude_pendiente(self):
        """Mismo principio con otra combinación real: Fraude + Cierre comparten
        smart_code pero son categorías distintas -- M3_FRAUD_AND_CLOSE (el
        cierre trae también datos de fraude) vs M3_FRAUD puro pendiente."""
        item_fraude = await self.queue_service.encolar_despacho(
            smart_code="SC-N1-3", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-N1-3", "tipo_fraude__c": "Suplantación"},
            error_inicial="SFC caída"
        )

        cancelado = await self.queue_service.cancelar_pendiente_por_smart_code(
            "SC-N1-3", operacion_actual="M3_CLOSE"
        )

        self.assertFalse(cancelado)
        self.assertIsNotNone(await self.redis.get(f"{QUEUE_PREFIX}:item:{item_fraude.id}"))


if __name__ == "__main__":
    unittest.main()
