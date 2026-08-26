# tests/test_idempotency_fail_closed.py
"""
Cobertura de la política FAIL-CLOSED de IdempotencyService.verificar_o_iniciar_operacion
-- antes sin cobertura directa en su rama activa (fail_closed=True, el default real):
sólo se probaba en otros archivos el manejo DOWNSTREAM del dict {"status":
"redis_unavailable", ...} ya construido (ej. routes_quejas.py), nunca el código que
realmente lo produce (_fail_closed_redis_no_disponible / _manejar_fallo_redis_idempotencia)
ni que dispare la alerta de infraestructura correspondiente.

También cubre dos ramas puntuales sin ejercitar:
- infer_operation_type cuando el payload es simultáneamente cierre Y fraude
  (M3_FRAUD_AND_CLOSE).
- _iniciar_registro_processing cuando el SET NX pierde la carrera contra otro
  proceso que ya completó la operación con el MISMO payload (debe devolver el
  hit exitoso ya persistido, no "processing").
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.idempotency_service import IdempotencyService
from app.services.email_service import EmailAlertService


class TestFailClosedRedisNoDisponible(unittest.IsolatedAsyncioTestCase):
    """redis_client=None -- IdempotencyService(redis_client=None)."""

    def setUp(self):
        self.payload = {"Smart_Code__c": "SC-1", "Status": "Closed"}
        self.idem = IdempotencyService(redis_client=None)

    async def test_fail_closed_true_bloquea_y_reporta_redis_unavailable(self):
        with patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alert:
            bloqueado, resultado = await self.idem.verificar_o_iniciar_operacion(
                smart_code="SC-1", payload_dict=self.payload, fail_closed=True
            )

        self.assertTrue(bloqueado)
        self.assertEqual(resultado["status"], "redis_unavailable")
        self.assertEqual(resultado["status_code"], 503)
        self.assertFalse(resultado["is_idempotent_hit"])
        self.assertEqual(resultado["error_type"], "IDEMPOTENCY_STORE_UNAVAILABLE")
        mock_alert.assert_awaited_once()
        self.assertEqual(mock_alert.await_args.kwargs["categoria"], "redis_no_disponible")

    async def test_fail_closed_false_no_bloquea_ni_reporta(self):
        with patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alert:
            bloqueado, resultado = await self.idem.verificar_o_iniciar_operacion(
                smart_code="SC-1", payload_dict=self.payload, fail_closed=False
            )

        self.assertFalse(bloqueado)
        self.assertIsNone(resultado)
        mock_alert.assert_not_awaited()


class TestFailClosedExcepcionDuranteConsulta(unittest.IsolatedAsyncioTestCase):
    """redis_client presente pero redis.get() lanza (timeout/conexión caída a mitad de vuelo)."""

    def setUp(self):
        self.payload = {"Smart_Code__c": "SC-2", "Status": "Closed"}
        self.mock_redis = MagicMock()
        self.mock_redis.get = AsyncMock(side_effect=ConnectionError("conexión perdida"))
        self.idem = IdempotencyService(self.mock_redis)

    async def test_fail_closed_true_bloquea_y_reporta_redis_unavailable(self):
        with patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alert:
            bloqueado, resultado = await self.idem.verificar_o_iniciar_operacion(
                smart_code="SC-2", payload_dict=self.payload, fail_closed=True
            )

        self.assertTrue(bloqueado)
        self.assertEqual(resultado["status"], "redis_unavailable")
        self.assertEqual(resultado["status_code"], 503)
        mock_alert.assert_awaited_once()
        self.assertEqual(mock_alert.await_args.kwargs["categoria"], "redis_no_disponible")

    async def test_fail_closed_false_no_bloquea_ni_reporta(self):
        with patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alert:
            bloqueado, resultado = await self.idem.verificar_o_iniciar_operacion(
                smart_code="SC-2", payload_dict=self.payload, fail_closed=False
            )

        self.assertFalse(bloqueado)
        self.assertIsNone(resultado)
        mock_alert.assert_not_awaited()


class TestInferOperationTypeFraudeYCierreSimultaneo(unittest.TestCase):

    def test_cierre_y_fraude_a_la_vez_clasifica_m3_fraud_and_close(self):
        payload = {
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Aceptada",
            "tipo_fraude__c": "Externo",
        }
        self.assertEqual(IdempotencyService.infer_operation_type(payload), "M3_FRAUD_AND_CLOSE")


class TestIniciarRegistroProcessingPierdeCarreraContraCompleted(unittest.IsolatedAsyncioTestCase):
    """
    SET NX falla porque OTRO proceso ya escribió el registro para el mismo
    smart_code+operation+payload_hash mientras este esperaba -- si ese registro
    ya quedó COMPLETED con el MISMO hash, debe devolverse como hit exitoso (con
    la respuesta ya persistida de la SFC), no como "processing" genérico.
    """

    async def test_devuelve_hit_exitoso_si_el_registro_ganador_ya_completo(self):
        import json

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=False)  # perdió la carrera SET NX
        mock_redis.get = AsyncMock(return_value=json.dumps({
            "status": "COMPLETED",
            "payload_hash": "hash-ganador",
            "sfc_response": {"codigo_queja_sfc": "SC-3"}
        }))

        idem = IdempotencyService(mock_redis)
        bloqueado, resultado = await idem._iniciar_registro_processing(
            key="{sfc:idempotency}:SC-3:M3_UPDATE:hash-ganador",
            source="CRM_SALESFORCE",
            smart_code="SC-3",
            operation="M3_UPDATE",
            payload_hash="hash-ganador",
            now_iso="2026-08-26T10:00:00-05:00"
        )

        self.assertTrue(bloqueado)
        self.assertEqual(resultado["status"], "success")
        self.assertTrue(resultado["is_idempotent_hit"])
        self.assertEqual(resultado["sfc_response"], {"codigo_queja_sfc": "SC-3"})

    async def test_devuelve_processing_si_el_registro_ganador_sigue_en_curso(self):
        import json

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=False)
        mock_redis.get = AsyncMock(return_value=json.dumps({
            "status": "PROCESSING",
            "payload_hash": "hash-ganador",
        }))

        idem = IdempotencyService(mock_redis)
        bloqueado, resultado = await idem._iniciar_registro_processing(
            key="{sfc:idempotency}:SC-4:M3_UPDATE:hash-ganador",
            source="CRM_SALESFORCE",
            smart_code="SC-4",
            operation="M3_UPDATE",
            payload_hash="hash-ganador",
            now_iso="2026-08-26T10:00:00-05:00"
        )

        self.assertTrue(bloqueado)
        self.assertEqual(resultado["status"], "processing")


if __name__ == "__main__":
    unittest.main()
