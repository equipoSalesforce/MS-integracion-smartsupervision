# tests/test_idempotency_queued_huerfano.py
import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
