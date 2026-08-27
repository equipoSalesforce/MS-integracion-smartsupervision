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

Primera corrección (2026-08-26, ronda temprana): ENQUEUE_LUA_SCRIPT rechazaba
la sobrescritura -- devolviendo un conflicto (409 QUEUE_OPERATION_CONFLICT del
lado de Python) en vez de pisar el contenido -- si la operación entrante
difería de la ya encolada. Cerraba la pérdida de datos, pero dejaba una
obligación regulatoria bloqueada detrás de la otra: con la SFC caída y dos
categorías pendientes para el mismo caso, la segunda en llegar quedaba
rechazada con 409 hasta que la primera drenara (hasta QUEUE_MAX_RETRIES
reintentos) -- y si el CRM no reintentaba persistentemente ante ese código
específico, esa obligación (que podía tener plazo regulatorio, ej. un cierre)
se perdía en la práctica.

🔴 FIX (hallazgo de revisión externa, 2026-08-26, ronda 4 -- X5/Y4): el índice
de cola pasó de "un smart_code = un slot" a "una obligación pendiente = un
slot" (`index:{smart_code}:{operacion}` en vez de `index:{smart_code}`). Dos
categorías de operación distintas para el mismo smart_code ya NO compiten por
el mismo slot -- cada una se encola de forma independiente, sin rechazo ni
pérdida. El `RedisLock` por caso (ver routes_quejas.py/scheduler.py) sigue
garantizando que nunca salgan dos operaciones del mismo smart_code a la vez
hacia la SFC, aunque ahora convivan en la cola. QUEUE_OPERATION_CONFLICT,
notificar_conflicto_operacion_cola y el 409 correspondiente se retiraron por
completo -- ya no pueden ocurrir por construcción.

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

    async def test_operacion_distinta_se_encola_por_separado_sin_rechazo(self):
        """El caso central de X5/Y4: fraude y trámite para el mismo smart_code ya
        no compiten por el mismo slot -- ambos se encolan, cada uno con su propio
        item_id y su propio índice particionado."""
        item_fraude = await self.queue_service.encolar_despacho(
            smart_code="SC-FRAUDE-1", tipo_operacion="AUTO",
            payload_json=self.payload_fraude, error_inicial="sfc caida"
        )
        item_tramite = await self.queue_service.encolar_despacho(
            smart_code="SC-FRAUDE-1", tipo_operacion="AUTO",
            payload_json=self.payload_tramite, error_inicial="sfc caida de nuevo"
        )

        self.assertNotEqual(item_fraude.id, item_tramite.id)
        self.assertFalse(item_tramite.es_duplicado)
        self.assertEqual(
            await self.redis.get(f"{QUEUE_PREFIX}:index:SC-FRAUDE-1:M3_FRAUD"), str(item_fraude.id)
        )
        self.assertEqual(
            await self.redis.get(f"{QUEUE_PREFIX}:index:SC-FRAUDE-1:M3_UPDATE"), str(item_tramite.id)
        )
        # Ambos quedan pendientes -- ninguno se pierde ni se rechaza.
        pendientes = await self.queue_service.obtener_todos_los_encolados(estado="PENDIENTE")
        self.assertEqual({r.id for r in pendientes}, {item_fraude.id, item_tramite.id})

    async def test_operacion_distinta_preserva_el_contenido_de_fraude(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-FRAUDE-1", tipo_operacion="AUTO",
            payload_json=self.payload_fraude, error_inicial="sfc caida"
        )
        await self.queue_service.encolar_despacho(
            smart_code="SC-FRAUDE-1", tipo_operacion="AUTO",
            payload_json=self.payload_tramite, error_inicial="sfc caida de nuevo"
        )

        # El item de fraude original -- mismo id, contenido intacto -- sigue ahí.
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

    async def test_tres_categorias_del_mismo_caso_conviven_en_slots_independientes(self):
        """Extiende el control anterior: dos trámites seguidos (misma categoría,
        mismo slot) más un fraude (categoría distinta, slot propio) para el mismo
        smart_code -- el fraude no debe verse afectado por las sobrescrituras del
        trámite, ni viceversa."""
        p1 = {"Smart_Code__c": "SC-C", "Status": "In Progress", "archivos_s3": []}
        p2 = {"Smart_Code__c": "SC-C", "Status": "In Progress", "archivos_s3": [], "sc_genero__c": "Masculino"}

        item1 = await self.queue_service.encolar_despacho(
            smart_code="SC-C", tipo_operacion="AUTO", payload_json=p1, error_inicial="e1"
        )
        item2 = await self.queue_service.encolar_despacho(
            smart_code="SC-C", tipo_operacion="AUTO", payload_json=p2, error_inicial="e2"
        )
        self.assertEqual(item2.id, item1.id)  # misma categoría, mismo slot

        fraude_sc_c = {**self.payload_fraude, "Smart_Code__c": "SC-C"}
        item_fraude = await self.queue_service.encolar_despacho(
            smart_code="SC-C", tipo_operacion="AUTO", payload_json=fraude_sc_c, error_inicial="e3"
        )

        self.assertNotEqual(item_fraude.id, item1.id)
        pendientes = await self.queue_service.obtener_todos_los_encolados(estado="PENDIENTE")
        self.assertEqual({r.id for r in pendientes}, {item1.id, item_fraude.id})

    async def test_item_legacy_bajo_la_key_sin_particion_no_se_encuentra_ni_se_pierde(self):
        """
        Compatibilidad hacia atrás (ronda 4 -- X5/Y4, riesgo de migración aceptado
        y documentado): un item encolado ANTES de este fix vive bajo la key SIN
        partición (`index:{smart_code}`, formato anterior). El código nuevo sólo
        consulta la key particionada (`index:{smart_code}:{operacion}`) -- no
        encuentra ese item legacy, así que un evento nuevo de la MISMA categoría
        no lo sobrescribe: crea un item independiente en vez de reutilizarlo.

        No es pérdida de datos: el item legacy sigue en Redis, sigue en el zset de
        pendientes, y el scheduler lo sigue reclamando y reintentando con
        normalidad -- sólo que ahora conviven temporalmente DOS items para lo que
        conceptualmente es la misma obligación, hasta que el legacy se resuelva
        (su índice viejo se limpia en MARK_SUCCESS/REGISTRAR_FALLO, ver esos
        scripts). En el peor caso, la SFC recibe el mismo PATCH dos veces -- que
        ya es idempotente por diseño (ver FLUJO_MOMENTOS.md), así que no genera un
        efecto duplicado real. Ventana acotada al período de transición del
        despliegue, no un estado permanente.
        """
        item_id = await self.redis.incr(f"{QUEUE_PREFIX}:counter")
        legacy_data = {
            "id": item_id, "smart_code": "SC-LEGACY-1", "tipo_operacion": "AUTO",
            "payload_json": json.dumps(self.payload_tramite), "payload_hash": "x",
            "estado": "PENDIENTE", "sfc_completado": False, "sfc_response": None,
            "intentos": 1, "max_intentos": 10, "ultimo_error": "e",
            "proximo_reintento_at": "2026-01-01T00:00:00", "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00", "correlation_id": "N/A",
            "es_duplicado": False, "version": 1
            # Sin 'operacion' a propósito -- item legacy real, de antes de N1.
        }
        await self.redis.set(f"{QUEUE_PREFIX}:item:{item_id}", json.dumps(legacy_data))
        await self.redis.sadd(f"{QUEUE_PREFIX}:status:PENDIENTE", item_id)
        await self.redis.zadd(f"{QUEUE_PREFIX}:pending_zset", {str(item_id): 123456})
        # Formato viejo, sin partición -- lo que un ENQUEUE pre-fix habría escrito.
        await self.redis.set(f"{QUEUE_PREFIX}:index:SC-LEGACY-1", item_id)

        tramite_nuevo = {"Smart_Code__c": "SC-LEGACY-1", "Status": "In Progress", "archivos_s3": []}
        item_nuevo = await self.queue_service.encolar_despacho(
            smart_code="SC-LEGACY-1", tipo_operacion="AUTO", payload_json=tramite_nuevo, error_inicial="e-nuevo"
        )

        # No lo reutiliza (la key vieja es invisible para el código nuevo) --
        # crea uno propio, particionado.
        self.assertNotEqual(item_nuevo.id, item_id)
        self.assertFalse(item_nuevo.es_duplicado)
        self.assertEqual(
            await self.redis.get(f"{QUEUE_PREFIX}:index:SC-LEGACY-1:M3_UPDATE"), str(item_nuevo.id)
        )
        # El item legacy NO se pierde -- sigue pendiente, el scheduler lo procesará.
        self.assertIsNotNone(await self.redis.get(f"{QUEUE_PREFIX}:item:{item_id}"))
        pendientes = await self.queue_service.obtener_todos_los_encolados(estado="PENDIENTE")
        self.assertEqual({r.id for r in pendientes}, {item_id, item_nuevo.id})

    async def test_operacion_inferida_persiste_para_la_proxima_comparacion(self):
        """Tras una sobrescritura exitosa (misma categoría), el campo 'operacion'
        guardado debe seguir reflejando la categoría vigente -- para que
        cancelar_pendiente_por_smart_code siga apuntando al slot correcto."""
        p1 = {"Smart_Code__c": "SC-D", "Status": "In Progress", "archivos_s3": []}
        p2 = {"Smart_Code__c": "SC-D", "Status": "In Progress", "archivos_s3": [], "sc_genero__c": "Masculino"}

        item1 = await self.queue_service.encolar_despacho(
            smart_code="SC-D", tipo_operacion="AUTO", payload_json=p1, error_inicial="e1"
        )
        item2 = await self.queue_service.encolar_despacho(
            smart_code="SC-D", tipo_operacion="AUTO", payload_json=p2, error_inicial="e2"
        )
        self.assertEqual(item2.id, item1.id)

        raw = await self.redis.get(f"{QUEUE_PREFIX}:item:{item1.id}")
        data = json.loads(raw)
        self.assertEqual(data["operacion"], "M3_UPDATE")


if __name__ == "__main__":
    unittest.main()
