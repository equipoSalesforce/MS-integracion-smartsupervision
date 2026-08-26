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
from app.services.idempotency_service import IdempotencyService
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

    # ==========================================================================
    # 🟢 Auditoría adversarial v9 (2026-08-17) — P0-01: marcar_sfc_completado
    # ==========================================================================

    async def test_overwrite_en_vuelo_no_marca_sfc_done_el_evento_nuevo(self):
        """
        P0-01: worker_1 reclama A (version=1); llega B del mismo smart_code
        (version=2) mientras A está en vuelo; worker_1 termina de procesar A ante
        la SFC e intenta marcar_sfc_completado con SU respuesta de SFC — debe
        rechazarse (version_mismatch) y NO debe persistirse sfc_completado=True
        sobre el contenido de B, que nunca fue enviado a la SFC.

        Nivel 1 (P0-02): tampoco debe quedar escrito el registro de idempotencia
        de A -- la validación de versión ocurre ANTES de esa escritura dentro del
        mismo script Lua, así que un rechazo por version_mismatch no deja rastro
        en el Idempotency Store.
        """
        payload_a = {"Smart_Code__c": "SC-500", "evento": "A"}
        item_a = await self.queue_service.encolar_despacho(
            smart_code="SC-500", tipo_operacion="AUTO",
            payload_json=payload_a, error_inicial="timeout inicial"
        )
        worker_1 = "worker_1"
        item_reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item_a.id, worker_id=worker_1, lease_segundos=60
        )
        self.assertEqual(item_reclamado.version, 1)

        item_b = await self.queue_service.encolar_despacho(
            smart_code="SC-500", tipo_operacion="AUTO",
            payload_json={"evento": "B"}, error_inicial="timeout B"
        )
        self.assertEqual(item_b.version, 2)

        resultado = await self.queue_service.marcar_sfc_completado(
            item_a.id, worker_id=worker_1, expected_version=item_reclamado.version,
            smart_code="SC-500", payload_dict=payload_a,
            sfc_response={"event_processed": "A"}
        )
        self.assertEqual(resultado, "version_mismatch")

        # El item vigente (B) NO debe quedar marcado como completado con la
        # respuesta de A — debe seguir pendiente para ser enviado de verdad.
        raw_item = await self.redis.get(f"{{sfc:queue}}:item:{item_a.id}")
        import json as _json
        data_vigente = _json.loads(raw_item)
        self.assertFalse(data_vigente["sfc_completado"])
        self.assertNotEqual(data_vigente.get("sfc_response"), {"event_processed": "A"})
        self.assertEqual(await self.queue_service.contar_pendientes(), 1)

        idem_key_a = IdempotencyService.construir_clave_completado("SC-500", payload_a)
        self.assertIsNone(await self.redis.get(idem_key_a))

    async def test_worker_sin_ownership_no_puede_marcar_sfc_done(self):
        """
        P0-01: un worker que perdió su lease no puede persistir SFC_DONE (recibe
        'not_owner'), y el nuevo dueño legítimo sí puede hacerlo con su propia
        respuesta de SFC.

        Nivel 1 (P0-02): el intento rechazado ('not_owner') tampoco debe dejar
        escrito el registro de idempotencia; sólo la escritura exitosa del nuevo
        dueño debe persistirlo, con el MISMO contenido y en la misma operación
        atómica que SFC_DONE.
        """
        payload = {"Smart_Code__c": "SC-600", "evento": "F"}
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-600", tipo_operacion="AUTO",
            payload_json=payload, error_inicial="timeout F"
        )
        worker_viejo = "worker_viejo"
        claim_viejo = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_viejo, lease_segundos=60
        )
        await self.redis.delete(f"{{sfc:queue}}:claim:{item.id}")

        worker_nuevo = "worker_nuevo"
        claim_nuevo = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_nuevo, lease_segundos=60
        )

        idem_key = IdempotencyService.construir_clave_completado("SC-600", payload)

        resultado_viejo = await self.queue_service.marcar_sfc_completado(
            item.id, worker_id=worker_viejo, expected_version=claim_viejo.version,
            smart_code="SC-600", payload_dict=payload,
            sfc_response={"event_processed": "viejo"}
        )
        self.assertEqual(resultado_viejo, "not_owner")
        self.assertIsNone(await self.redis.get(idem_key))

        resultado_nuevo = await self.queue_service.marcar_sfc_completado(
            item.id, worker_id=worker_nuevo, expected_version=claim_nuevo.version,
            smart_code="SC-600", payload_dict=payload,
            sfc_response={"event_processed": "nuevo"}
        )
        self.assertEqual(resultado_nuevo, "completed")

        raw_item = await self.redis.get(f"{{sfc:queue}}:item:{item.id}")
        import json as _json
        data = _json.loads(raw_item)
        self.assertTrue(data["sfc_completado"])
        # 🔴 FIX (hallazgo N4, revisión externa v5, 2026-08-25): sfc_response se guarda
        # como string JSON opaco dentro del item de cola (no en el registro de
        # idempotencia de abajo, que siempre fue Python-serializado y no se ve afectado).
        self.assertEqual(_json.loads(data["sfc_response"]), {"event_processed": "nuevo"})

        idem_raw = await self.redis.get(idem_key)
        self.assertIsNotNone(idem_raw, "El registro de idempotencia debe quedar escrito junto con SFC_DONE")
        idem_data = _json.loads(idem_raw)
        self.assertEqual(idem_data["status"], "COMPLETED")
        self.assertEqual(idem_data["sfc_response"], {"event_processed": "nuevo"})

    async def test_marcar_sfc_completado_persiste_idempotencia_atomicamente_con_sfc_done(self):
        """
        Nivel 1 (auditoría adversarial v10, P0-02): caso base sin carrera -- un
        único worker que completa SFC_DONE debe ver el registro de idempotencia
        COMPLETED aparecer en la MISMA llamada, no como un paso separado que
        pueda fallar independientemente (que era exactamente el escenario de
        P0-02: idempotencia COMPLETED sin SFC_DONE, o viceversa).
        """
        payload = {"Smart_Code__c": "SC-650", "evento": "atomico"}
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-650", tipo_operacion="AUTO",
            payload_json=payload, error_inicial="timeout inicial"
        )
        worker_id = "worker_atomico"
        claim = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_id, lease_segundos=60
        )

        idem_key = IdempotencyService.construir_clave_completado("SC-650", payload)
        self.assertIsNone(await self.redis.get(idem_key), "No debe existir antes de completar")

        resultado = await self.queue_service.marcar_sfc_completado(
            item.id, worker_id=worker_id, expected_version=claim.version,
            smart_code="SC-650", payload_dict=payload,
            sfc_response={"codigo_queja": "SC-650", "estado_cod": 4}
        )
        self.assertEqual(resultado, "completed")

        import json as _json
        idem_raw = await self.redis.get(idem_key)
        self.assertIsNotNone(idem_raw)
        idem_data = _json.loads(idem_raw)
        self.assertEqual(idem_data["status"], "COMPLETED")
        self.assertEqual(idem_data["smart_code"], "SC-650")
        self.assertEqual(idem_data["sfc_response"], {"codigo_queja": "SC-650", "estado_cod": 4})

        # TTL real (no -1/persistente ni -2/inexistente) -- confirma que el PX se
        # aplicó dentro del script Lua, no que quedó sin expiración.
        ttl = await self.redis.pttl(idem_key)
        self.assertGreater(ttl, 0)

    # ==========================================================================
    # 🟢 Auditoría adversarial v9 (2026-08-17) — P0-02: registrar_fallo
    # ==========================================================================

    async def test_overwrite_en_vuelo_no_aplica_fallo_al_evento_nuevo(self):
        """
        P0-02: worker_1 reclama A (version=1); llega B del mismo smart_code
        (version=2) mientras A está en vuelo; A falla y worker_1 llama
        registrar_fallo — debe rechazarse (version_mismatch) sin incrementar
        intentos ni escribir el error de A sobre el contenido vigente de B.
        """
        item_a = await self.queue_service.encolar_despacho(
            smart_code="SC-700", tipo_operacion="AUTO",
            payload_json={"evento": "A"}, error_inicial="timeout inicial"
        )
        worker_1 = "worker_1"
        item_reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item_a.id, worker_id=worker_1, lease_segundos=60
        )
        self.assertEqual(item_reclamado.intentos, 1)

        item_b = await self.queue_service.encolar_despacho(
            smart_code="SC-700", tipo_operacion="AUTO",
            payload_json={"evento": "B"}, error_inicial="timeout B"
        )
        self.assertEqual(item_b.version, 2)

        resultado = await self.queue_service.registrar_fallo(
            item=item_reclamado, error_msg="ERROR FROM EVENT A", worker_id=worker_1
        )
        self.assertEqual(resultado, "version_mismatch")

        raw_item = await self.redis.get(f"{{sfc:queue}}:item:{item_a.id}")
        import json as _json
        data_vigente = _json.loads(raw_item)
        self.assertEqual(data_vigente["intentos"], item_b.intentos, "No debe incrementarse sobre el contenido de B")
        self.assertNotEqual(data_vigente["ultimo_error"], "ERROR FROM EVENT A")
        self.assertEqual(data_vigente["version"], 2)

    async def test_worker_sin_ownership_no_puede_registrar_fallo_ni_robar_claim(self):
        """
        P0-02: un worker que perdió su lease no puede registrar un fallo (recibe
        'not_owner') ni borrar el claim del nuevo dueño; el nuevo dueño sí puede
        registrar su propio fallo con normalidad.
        """
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-800", tipo_operacion="AUTO",
            payload_json={"evento": "G"}, error_inicial="timeout G"
        )
        worker_viejo = "worker_viejo"
        claim_viejo = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_viejo, lease_segundos=60
        )
        await self.redis.delete(f"{{sfc:queue}}:claim:{item.id}")

        worker_nuevo = "worker_nuevo"
        claim_nuevo = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id=worker_nuevo, lease_segundos=60
        )

        resultado_viejo = await self.queue_service.registrar_fallo(
            item=claim_viejo, error_msg="ERROR FROM WORKER VIEJO", worker_id=worker_viejo
        )
        self.assertEqual(resultado_viejo, "not_owner")

        # El claim del worker nuevo debe seguir intacto tras el intento del viejo.
        claim_actual = await self.redis.get(f"{{sfc:queue}}:claim:{item.id}")
        self.assertEqual(claim_actual, worker_nuevo)

        resultado_nuevo = await self.queue_service.registrar_fallo(
            item=claim_nuevo, error_msg="ERROR FROM WORKER NUEVO", worker_id=worker_nuevo
        )
        self.assertEqual(resultado_nuevo, "failed")

        raw_item = await self.redis.get(f"{{sfc:queue}}:item:{item.id}")
        import json as _json
        data = _json.loads(raw_item)
        self.assertEqual(data["intentos"], 2)
        self.assertEqual(data["ultimo_error"], "ERROR FROM WORKER NUEVO")

    # ==========================================================================
    # 🟢 Observabilidad (auditoría adversarial v9, sección 6): estado PROCESSING
    # ==========================================================================

    async def test_reclamar_marca_estado_processing(self):
        """SmartStatus.PROCESSING estaba declarado pero nunca se aplicaba: el campo
        `estado` se quedaba en PENDING durante todo el procesamiento, indistinguible
        de un item nunca reclamado. Al reclamar, debe reflejarse PROCESSING."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-900", tipo_operacion="AUTO",
            payload_json={"evento": "H"}, error_inicial="timeout H"
        )
        self.assertEqual(item.estado, SmartStatus.PENDING.value)

        claim = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        self.assertEqual(claim.estado, SmartStatus.PROCESSING.value)

        raw_item = await self.redis.get(f"{{sfc:queue}}:item:{item.id}")
        import json as _json
        self.assertEqual(_json.loads(raw_item)["estado"], SmartStatus.PROCESSING.value)

    async def test_fallo_no_definitivo_regresa_estado_a_pending(self):
        """Tras un fallo que todavía no es definitivo, el item vuelve a esperar su
        próximo turno de reintento -- ya no está "en proceso", así que el estado no
        debe quedarse en PROCESSING hasta el siguiente ciclo que lo reclame."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-901", tipo_operacion="AUTO",
            payload_json={"evento": "I"}, error_inicial="timeout I"
        )
        claim = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        self.assertEqual(claim.estado, SmartStatus.PROCESSING.value)

        resultado = await self.queue_service.registrar_fallo(
            item=claim, error_msg="fallo transitorio", worker_id="worker_1"
        )
        self.assertEqual(resultado, "failed")

        raw_item = await self.redis.get(f"{{sfc:queue}}:item:{item.id}")
        import json as _json
        self.assertEqual(_json.loads(raw_item)["estado"], SmartStatus.PENDING.value)

    async def test_registrar_fallo_con_consumir_intento_false_no_incrementa_intentos(self):
        """
        Fallo del webhook al CRM clasificado como caída de infraestructura (ver
        scheduler.py:_es_falla_infraestructura): la SFC ya proceso el caso con
        éxito, así que este fallo NO debe consumir el presupuesto de reintentos ni
        poder marcar el registro como definitivo/DLQ.
        """
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-903", tipo_operacion="AUTO",
            payload_json={"evento": "J"}, error_inicial="timeout J"
        )
        intentos_iniciales = item.intentos

        claim = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        resultado = await self.queue_service.registrar_fallo(
            item=claim, error_msg="CRM Webhook: 503 Service Unavailable",
            worker_id="worker_1", consumir_intento=False
        )
        self.assertEqual(resultado, "failed")

        raw_item = await self.redis.get(f"{{sfc:queue}}:item:{item.id}")
        import json as _json
        data_vigente = _json.loads(raw_item)
        self.assertEqual(data_vigente["intentos"], intentos_iniciales)
        self.assertEqual(data_vigente["estado"], SmartStatus.PENDING.value)

    async def test_overwrite_no_hereda_estado_processing(self):
        """Si un evento B sobrescribe a A mientras A está PROCESSING, el contenido
        vigente (B) todavía no fue reclamado por nadie -- debe partir en PENDING, no
        heredar el PROCESSING del contenido anterior."""
        item_a = await self.queue_service.encolar_despacho(
            smart_code="SC-902", tipo_operacion="AUTO",
            payload_json={"evento": "A"}, error_inicial="timeout inicial"
        )
        await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item_a.id, worker_id="worker_1", lease_segundos=60
        )

        item_b = await self.queue_service.encolar_despacho(
            smart_code="SC-902", tipo_operacion="AUTO",
            payload_json={"evento": "B"}, error_inicial="timeout B"
        )
        self.assertEqual(item_b.version, 2)
        self.assertEqual(item_b.estado, SmartStatus.PENDING.value)


if __name__ == "__main__":
    unittest.main()
