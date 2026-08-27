# tests/test_idempotency_queued_huerfano.py
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services.idempotency_service import IdempotencyService


class _StubRedis:
    """Emula GET/SET(nx)/DELETE sobre un dict en memoria, suficiente para ejercitar
    verificar_o_iniciar_operacion / registrar_encolado sin un Redis real."""

    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, px=None, nx=None, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)


class TestIdempotencyQueuedHuerfano(unittest.IsolatedAsyncioTestCase):
    """🟢 FIX P0-12: un registro de idempotencia QUEUED no debe reportarse como vigente
    si el item de cola que referencia ya fue sobrescrito por un evento más nuevo del
    mismo smart_code (o ya no existe / ya no está pendiente)."""

    def setUp(self):
        self.redis = _StubRedis()
        self.idem = IdempotencyService(self.redis)
        self.payload_a = {"Smart_Code__c": "SC-1", "Status": "In Progress", "tipo_fraude__c": "Externo"}

    async def _encolar_a(self, registro_id=42):
        await self.idem.verificar_o_iniciar_operacion("SC-1", self.payload_a)
        await self.idem.registrar_encolado("SC-1", self.payload_a, error_msg="SFC caida", registro_id=registro_id)

    async def test_registro_huerfano_se_libera_y_deja_pasar_como_nueva_operacion(self):
        """Si el item de cola fue sobrescrito por otro evento (payload distinto), un
        retry del request original A ya no debe reportarse como 'already_queued'."""
        await self._encolar_a()

        payload_b = {
            "Smart_Code__c": "SC-1", "Status": "Closed",
            "tipo_fraude__c": "Externo", "Favorabilidad__c": "Favorable"
        }
        self.redis.store["{sfc:queue}:item:42"] = json.dumps({
            "estado": "PENDIENTE", "payload_json": payload_b, "smart_code": "SC-1"
        })

        es_hit, respuesta = await self.idem.verificar_o_iniciar_operacion("SC-1", self.payload_a)

        self.assertFalse(es_hit, "El registro QUEUED huérfano no debe reportarse como vigente")

    async def test_registro_huerfano_si_item_ya_no_esta_pendiente(self):
        """Si el item de cola ya fue completado/falló definitivamente, tampoco debe
        seguir reportando 'already_queued'."""
        await self._encolar_a()

        self.redis.store["{sfc:queue}:item:42"] = json.dumps({
            "estado": "COMPLETED", "payload_json": self.payload_a, "smart_code": "SC-1"
        })

        es_hit, _ = await self.idem.verificar_o_iniciar_operacion("SC-1", self.payload_a)

        self.assertFalse(es_hit)

    async def test_registro_sigue_vigente_sin_overwrite_no_hay_falso_negativo(self):
        """Caso normal: si el item de cola sigue con el MISMO payload, el hit
        'already_queued' debe seguir reportándose (no romper el comportamiento sano)."""
        await self._encolar_a()

        self.redis.store["{sfc:queue}:item:42"] = json.dumps({
            "estado": "PENDIENTE", "payload_json": self.payload_a, "smart_code": "SC-1"
        })

        es_hit, respuesta = await self.idem.verificar_o_iniciar_operacion("SC-1", self.payload_a)

        self.assertTrue(es_hit)
        self.assertEqual(respuesta["status"], "already_queued")

    async def test_registros_queued_previos_al_fix_sin_queue_item_id_se_asumen_vigentes(self):
        """Registros QUEUED escritos antes de este fix (sin queue_item_id) no deben
        romperse: se asumen vigentes por compatibilidad hacia atrás."""
        operation = self.idem.infer_operation_type(self.payload_a)
        payload_hash = self.idem.compute_payload_hash(self.payload_a)
        key = self.idem._get_idempotency_key("SC-1", operation, payload_hash)
        self.redis.store[key] = json.dumps({
            "smart_code": "SC-1", "operation": operation, "payload_hash": payload_hash,
            "status": "QUEUED", "queue_item_id": None
        })

        es_hit, respuesta = await self.idem.verificar_o_iniciar_operacion("SC-1", self.payload_a)

        self.assertTrue(es_hit)
        self.assertEqual(respuesta["status"], "already_queued")


class _StubRedisDeleteFalla(_StubRedis):
    """Igual que _StubRedis, pero DELETE siempre falla -- emula una caída
    transitoria de Redis justo al intentar liberar un registro QUEUED huérfano."""

    async def delete(self, key):
        raise ConnectionError("redis down during orphaned QUEUED cleanup")


class TestIdempotencyQueuedHuerfanoFalloAlLiberar(unittest.IsolatedAsyncioTestCase):
    """
    🔴 FIX (hallazgo propio, 2026-08-27): antes, si el DELETE del registro QUEUED
    huérfano fallaba (error transitorio de Redis), sólo se loggeaba un warning y el
    código caía igual hacia _iniciar_registro_processing como si la limpieza hubiera
    funcionado. El SET...NX de ahí fallaba contra la key huérfana que en realidad
    seguía existiendo, y el método devolvía un falso "status": "processing" --
    bloqueando una operación legítima nueva hasta por el TTL completo del registro
    QUEUED (hasta 30 días), sin ninguna alerta. Debe fallar cerrado (503) como
    cualquier otro error de Redis en este flujo, no devolver un falso positivo."""

    def setUp(self):
        self.redis = _StubRedisDeleteFalla()
        self.idem = IdempotencyService(self.redis)
        self.payload_a = {"Smart_Code__c": "SC-1", "Status": "In Progress", "tipo_fraude__c": "Externo"}

    async def test_fallo_al_liberar_registro_huerfano_falla_cerrado_no_falso_processing(self):
        await self.idem.verificar_o_iniciar_operacion("SC-1", self.payload_a)
        await self.idem.registrar_encolado("SC-1", self.payload_a, error_msg="SFC caida", registro_id=42)

        payload_b = {
            "Smart_Code__c": "SC-1", "Status": "Closed",
            "tipo_fraude__c": "Externo", "Favorabilidad__c": "Favorable"
        }
        self.redis.store["{sfc:queue}:item:42"] = json.dumps({
            "estado": "PENDIENTE", "payload_json": payload_b, "smart_code": "SC-1"
        })

        with patch("app.services.idempotency_service.EmailAlertService.notificar_falla_infraestructura", new=AsyncMock()):
            es_hit, respuesta = await self.idem.verificar_o_iniciar_operacion("SC-1", self.payload_a)

        self.assertTrue(es_hit)
        self.assertEqual(respuesta["status"], "redis_unavailable")
        self.assertEqual(respuesta["status_code"], 503)
        self.assertNotEqual(respuesta["status"], "processing")


class _StubRedisSetFalla(_StubRedis):
    """Igual que _StubRedis, pero SET siempre falla — emula una caída de Redis en el
    momento exacto de persistir el estado QUEUED."""

    async def set(self, key, value, px=None, nx=None, ex=None):
        raise ConnectionError("redis down during QUEUED write")


class TestRegistrarEncoladoFallaPersistencia(unittest.IsolatedAsyncioTestCase):
    """🟢 FIX P0-03 (auditoría adversarial v9): si falla la escritura idempotente
    QUEUED, el método debe propagar el error (tras reintentar) en vez de tragárselo
    y retornar como si hubiera quedado protegido. El caller real (routes_quejas.py)
    ya envuelve esta llamada en el mismo try/except que usa para el fallo doble
    SFC+Redis de encolar_despacho, así que esta propagación es lo que evita que el
    endpoint responda 202 sin que la barrera de idempotencia esté realmente activa."""

    def setUp(self):
        self.redis = _StubRedisSetFalla()
        self.idem = IdempotencyService(self.redis)
        self.payload_a = {"Smart_Code__c": "SC-1", "Status": "In Progress", "tipo_fraude__c": "Externo"}

    async def test_registrar_encolado_propaga_tras_agotar_reintentos(self):
        with patch("app.services.idempotency_service.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError) as ctx:
                await self.idem.registrar_encolado(
                    "SC-1", self.payload_a, error_msg="SFC caida", registro_id=42
                )
        self.assertIn("SC-1", str(ctx.exception))

    async def test_registrar_encolado_sin_redis_lanza_de_inmediato(self):
        idem_sin_redis = IdempotencyService(redis_client=None)
        with self.assertRaises(RuntimeError):
            await idem_sin_redis.registrar_encolado(
                "SC-1", self.payload_a, error_msg="SFC caida", registro_id=42
            )


if __name__ == "__main__":
    unittest.main()
