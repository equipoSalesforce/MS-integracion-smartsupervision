# tests/test_idempotency_encolado_directorio_s3_consistente.py
"""
Regresión de un hallazgo de revisión externa (2026-08-25): el payload_json que
encolar_despacho() guardaba en la cola de contingencia se calculaba DESPUÉS de
que DespachoQuejaOrquestador.procesar_despacho mutara payload.archivos_s3 al
resolver directorio_s3 -- pero un reintento genuino del CRM (mismo request
original) siempre calcula su propio raw_payload ANTES de esa mutación, porque
resolver directorio_s3 es un efecto interno de este servicio, invisible para
el CRM.

IdempotencyService._item_de_cola_sigue_vigente compara el hash del
payload_json ACTUAL del item de cola contra el hash del payload entrante del
reintento -- con el item guardado en su versión post-mutación (con
archivos_s3 ya resuelto) y el reintento siempre pre-mutación (archivos_s3
vacío), esos hashes NUNCA coincidían para ningún caso con directorio_s3. El
registro QUEUED se trataba siempre como huérfano, anulando la barrera
anti-duplicado que P0-12 (test_idempotency_queued_huerfano.py) buscaba
construir, justo para ese subconjunto de casos.

El fix: encolar_despacho() debe guardar el mismo snapshot PRE-mutación que
registrar_encolado() ya usaba (y que un reintento real volverá a producir),
no un payload.model_dump() recalculado después de la orquestación.

Se prueba contra Redis real -- QueueService e IdempotencyService interactúan
a través de claves reales de Redis, no de un stub de la estructura interna.
"""
import os
import unittest

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService
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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo la regresión de idempotencia QUEUED "
    "con directorio_s3. Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) "
    "para ejecutarla."
)
class TestEncoladoConDirectorioS3MantieneIdempotenciaQueued(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)
        self.idempotency_service = IdempotencyService(redis_client=self.redis)

        # Snapshot PRE-mutación: lo que el CRM realmente envía y lo que un
        # reintento futuro volverá a enviar -- directorio_s3 presente,
        # archivos_s3 todavía vacío (se resuelve server-side).
        self.raw_payload = {
            "Smart_Code__c": "SC-DIR-1",
            "Status": "New",
            "directorio_s3": "caso/SC-DIR-1/",
            "archivos_s3": []
        }

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def _simular_encolado_por_contingencia(self, payload_para_la_cola: dict, registro_id_previo=None):
        """Reproduce lo que hace _encolar_despacho_por_contingencia tras el fix:
        la MISMA llamada de payload_json para encolar_despacho() y payload_dict
        para registrar_encolado()."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-DIR-1", tipo_operacion="AUTO",
            payload_json=payload_para_la_cola, error_inicial="SFC caida"
        )
        await self.idempotency_service.registrar_encolado(
            smart_code="SC-DIR-1", payload_dict=payload_para_la_cola,
            error_msg="SFC caida", registro_id=item.id
        )
        return item

    async def test_reintento_del_mismo_request_original_se_reconoce_como_already_queued(self):
        """El escenario central del fix: tras encolar (con directorio_s3 resuelto
        internamente), un reintento del CRM con el body ORIGINAL (pre-mutación) debe
        reconocerse como 'already_queued', no procesarse como una operación nueva."""
        # 1. Primera vez: idempotencia arranca en PROCESSING sobre el request original.
        es_hit_inicial, _ = await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload
        )
        self.assertFalse(es_hit_inicial)

        # 2. SFC falla -> se encola. El fix: se usa el snapshot PRE-mutación (raw_payload),
        #    no un dump recalculado después de resolver directorio_s3.
        await self._simular_encolado_por_contingencia(self.raw_payload)

        # 3. Reintento del CRM: mismo request original, recién parseado -- pre-mutación
        #    por definición, ya que la resolución de directorio_s3 nunca ocurrió para
        #    ESTE nuevo intento (falló antes de llegar ahí).
        es_hit_reintento, respuesta = await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload
        )

        self.assertTrue(es_hit_reintento, "El reintento debe reconocerse como ya encolado, no como huérfano")
        self.assertEqual(respuesta["status"], "already_queued")

    async def test_regresion_si_se_guardara_el_payload_post_mutacion_el_reintento_se_veria_huerfano(self):
        """
        Prueba negativa que documenta el bug real: si encolar_despacho() hubiera
        recibido el payload YA MUTADO (con archivos_s3 resuelto, como hacía el código
        antes del fix) en vez del snapshot pre-mutación, el mismo reintento del punto
        anterior se habría tratado como huérfano -- exactamente el bug reportado.
        """
        payload_post_mutacion = dict(self.raw_payload)
        payload_post_mutacion["archivos_s3"] = [
            {"nombre_archivo": "soporte.pdf", "s3_key": "caso/SC-DIR-1/soporte.pdf", "bucket": "b"}
        ]

        es_hit_inicial, _ = await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload
        )
        self.assertFalse(es_hit_inicial)

        # Comportamiento ANTERIOR al fix: encolar_despacho() guarda el payload YA
        # mutado (con archivos_s3 resuelto) mientras registrar_encolado() sigue
        # recibiendo raw_payload (pre-mutación) -- reproduce la divergencia real.
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-DIR-1", tipo_operacion="AUTO",
            payload_json=payload_post_mutacion, error_inicial="SFC caida"
        )
        await self.idempotency_service.registrar_encolado(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload,
            error_msg="SFC caida", registro_id=item.id
        )

        es_hit_reintento, _ = await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload
        )

        self.assertFalse(
            es_hit_reintento,
            "Con el bug real, el reintento NO se reconoce como encolado -- se trata como huérfano/nuevo"
        )

    async def test_operacion_completada_desde_la_cola_sigue_reconociendose_como_exito(self):
        """El resto del ciclo de vida (QUEUED -> COMPLETED vía marcar_sfc_completado)
        no debe romperse por este fix: sigue usando el mismo raw_payload consistente."""
        await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload
        )
        item = await self._simular_encolado_por_contingencia(self.raw_payload)

        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        resultado = await self.queue_service.marcar_sfc_completado(
            reclamado.id, worker_id="worker_1", expected_version=reclamado.version,
            smart_code="SC-DIR-1", payload_dict=reclamado.payload_json,
            sfc_response={"status": "success"}, payload_hash=reclamado.payload_hash
        )
        self.assertEqual(resultado, "completed")

        es_hit, respuesta = await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload
        )
        self.assertTrue(es_hit)
        self.assertEqual(respuesta["status"], "success")

    async def test_sin_payload_hash_marcar_sfc_completado_igual_se_reconoce_como_exito(self):
        """
        🔴 FIX N4 (revisión externa v5, 2026-08-25): esta prueba era negativa --
        documentaba que, si marcar_sfc_completado() no recibía payload_hash, el
        registro COMPLETED se calculaba sobre `reclamado.payload_json` -- el
        payload_json YA ALMACENADO en la cola, que para este caso tenía
        `archivos_s3: []` corrompido a `{}` por el round-trip de cjson en
        ENQUEUE_LUA_SCRIPT. Ese hash nunca coincidía con el de `self.raw_payload`
        (con archivos_s3 como lista real), así que un reintento del CRM tras la
        confirmación NO se reconocía como ya exitoso.

        Con el fix (payload_json se guarda como string JSON opaco, inmune al
        round-trip de cjson), `reclamado.payload_json` ya no está corrompido --
        el fallback sin payload_hash explícito ahora SÍ reconoce el reintento
        como exitoso, igual que si se hubiera pasado el hash explícitamente.
        payload_hash sigue siendo la vía preferida (evita recalcular), pero
        dejó de ser la única forma de que esto funcione correctamente.
        """
        await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload
        )
        item = await self._simular_encolado_por_contingencia(self.raw_payload)
        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        # Deliberadamente sin payload_hash -- cae al recálculo desde
        # reclamado.payload_json, que ya no está corrompido tras el fix de N4.
        resultado = await self.queue_service.marcar_sfc_completado(
            reclamado.id, worker_id="worker_1", expected_version=reclamado.version,
            smart_code="SC-DIR-1", payload_dict=reclamado.payload_json,
            sfc_response={"status": "success"}
        )
        self.assertEqual(resultado, "completed")

        es_hit, respuesta = await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload
        )
        self.assertTrue(
            es_hit,
            "Sin el bug de N4, el reintento SÍ debe reconocerse como ya exitoso "
            "aunque marcar_sfc_completado no reciba payload_hash explícito"
        )
        self.assertEqual(respuesta["status"], "success")

    async def test_evento_nuevo_con_contenido_distinto_sigue_sobrescribiendo_normalmente(self):
        """El fix no debe afectar el camino ya cubierto (sobrescritura por contenido
        realmente nuevo, no un reintento del mismo request): un smart_code distinto en
        contenido debe seguir usando su propio hash y no verse afectado."""
        await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=self.raw_payload
        )
        await self._simular_encolado_por_contingencia(self.raw_payload)

        payload_v2 = dict(self.raw_payload)
        payload_v2["Status"] = "Closed"

        es_hit_v2, _ = await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-DIR-1", payload_dict=payload_v2
        )
        self.assertFalse(es_hit_v2, "Contenido genuinamente distinto no debe reportarse como ya encolado")


if __name__ == "__main__":
    unittest.main()
