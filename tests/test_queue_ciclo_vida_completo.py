# tests/test_queue_ciclo_vida_completo.py
"""
Auditoría de cobertura (2026-08-26): cada transición individual de la máquina de
estados de la cola tiene su propio test (encolar, reclamar, fallar, caer a DLQ,
reencolar, completar) -- pero ningún test existente encadena TODAS en un solo caso
de punta a punta. Los bugs de integración entre piezas individualmente correctas
(un estado que no se limpia bien al pasar a la siguiente etapa, un campo que se
queda "pegado" de una fase anterior) son justo el tipo que las pruebas aisladas no
atrapan y un test de ciclo de vida completo sí.

Recorre: encolar -> reclamar -> falla transitoria (PENDIENTE de nuevo) -> reclamar ->
falla transitoria otra vez -> reclamar -> falla definitiva (FALLIDO_DEFINITIVO/DLQ,
hallazgo N1-adyacente: se libera idempotencia y se alerta) -> reencolado
administrativo (hallazgo C2) -> reclamar -> éxito real ante la SFC
(marcar_sfc_completado, hallazgo N4/P0-01/P0-02: persiste SFC_DONE + idempotencia
COMPLETED atómicamente).
"""
import json
import os
import unittest
from unittest.mock import patch, AsyncMock

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, QUEUE_PREFIX
from app.services.idempotency_service import IdempotencyService
from app.core.config import settings
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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo el test de ciclo de vida "
    "completo. Levante un Redis local (ej. `docker run --rm -p 6379:6379 "
    "redis:7-alpine`) para ejecutarlo."
)
class TestCicloDeVidaCompletoDeUnCaso(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)
        self.idempotency_service = IdempotencyService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_encolar_fallar_dos_veces_caer_a_dlq_reencolar_y_completar(self):
        smart_code = "SC-CICLO-1"
        payload = {"Smart_Code__c": smart_code, "Status": "In Progress"}

        # intentos arranca en 1 desde encolar_despacho (representa el intento
        # síncrono inicial que ya falló y disparó el encolado) -- con
        # QUEUE_MAX_RETRIES=3, un solo registrar_fallo transitorio (1->2, 2>=3 es
        # falso, no agota todavía) seguido de uno definitivo (2->3, 3>=3 es
        # verdadero, agota) reproduce el ciclo completo con el mínimo de pasos.
        with patch.object(settings, "QUEUE_MAX_RETRIES", 3):
            # --- 1. Encolado inicial (SFC caída/lenta) ---
            item = await self.queue_service.encolar_despacho(
                smart_code=smart_code, tipo_operacion="AUTO",
                payload_json=payload, error_inicial="503 SFC"
            )
            await self.idempotency_service.registrar_encolado(
                smart_code=smart_code, payload_dict=payload,
                error_msg="503 SFC", registro_id=item.id
            )
            self.assertEqual(await self.queue_service.contar_pendientes(), 1)

            # --- 2. Reclamo + falla transitoria (no agota el límite todavía) ---
            claim = await self.queue_service.reclamar_item_para_procesamiento(
                registro_id=item.id, worker_id="worker_1", lease_segundos=60
            )
            self.assertIsNotNone(claim)
            self.assertEqual(claim.estado, SmartStatus.PROCESSING.value)

            resultado_fallo = await self.queue_service.registrar_fallo(
                item=claim, error_msg="fallo transitorio", worker_id="worker_1"
            )
            self.assertEqual(resultado_fallo, "failed")

            raw_item = await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}")
            self.assertEqual(json.loads(raw_item)["estado"], SmartStatus.PENDING.value)
            self.assertEqual(json.loads(raw_item)["intentos"], 2)

            # --- 3. Segundo intento: agota QUEUE_MAX_RETRIES -> DLQ ---
            claim_final = await self.queue_service.reclamar_item_para_procesamiento(
                registro_id=item.id, worker_id="worker_2", lease_segundos=60
            )
            with patch(
                "app.services.queue_service.EmailAlertService.notificar_caso_fallido_definitivo",
                new_callable=AsyncMock
            ) as mock_alerta_dlq:
                resultado_definitivo = await self.queue_service.registrar_fallo(
                    item=claim_final, error_msg="fallo definitivo", worker_id="worker_2"
                )
            self.assertEqual(resultado_definitivo, "failed")
            mock_alerta_dlq.assert_awaited_once()

        # El caso cayó a la DLQ: fuera de pendientes, idempotencia liberada, sin índice.
        self.assertEqual(await self.queue_service.contar_pendientes(), 0)
        self.assertEqual(await self.redis.scard(f"{QUEUE_PREFIX}:status:FALLIDO_DEFINITIVO"), 1)
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:index:{smart_code}:M3_UPDATE"))
        idem_key_queued = self.idempotency_service._get_idempotency_key(
            smart_code, IdempotencyService.infer_operation_type(payload),
            IdempotencyService.compute_payload_hash(payload)
        )
        self.assertIsNone(
            await self.redis.get(idem_key_queued),
            "El registro QUEUED debe haberse liberado al caer a FALLIDO_DEFINITIVO"
        )

        # --- 5. Un operador lo reencola manualmente (hallazgo C2) ---
        resultado_replay = await self.queue_service.reencolar_item_fallido(item.id)
        self.assertTrue(resultado_replay["success"])
        self.assertEqual(await self.queue_service.contar_pendientes(), 1)
        self.assertEqual(await self.redis.get(f"{QUEUE_PREFIX}:index:{smart_code}:M3_UPDATE"), str(item.id))

        raw_item_reencolado = await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}")
        data_reencolada = json.loads(raw_item_reencolado)
        self.assertEqual(data_reencolada["intentos"], 0, "El reencolado administrativo reinicia el presupuesto de reintentos")
        self.assertEqual(data_reencolada["estado"], SmartStatus.PENDING.value)

        # El reencolado debe haber reconstruido un registro QUEUED en idempotencia.
        raw_idem_queued = await self.redis.get(idem_key_queued)
        self.assertIsNotNone(raw_idem_queued)
        self.assertEqual(json.loads(raw_idem_queued)["status"], "QUEUED")

        # --- 6. Esta vez, éxito real ante la SFC ---
        claim_exitoso = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_final", lease_segundos=60
        )
        self.assertIsNotNone(claim_exitoso)

        resultado_sfc = await self.queue_service.marcar_sfc_completado(
            item.id, worker_id="worker_final", expected_version=claim_exitoso.version,
            smart_code=smart_code, payload_dict=payload,
            sfc_response={"codigo_queja": smart_code, "estado_cod": 2}
        )
        self.assertEqual(resultado_sfc, "completed")

        # marcar_sfc_completado sólo persiste que la SFC ya aceptó el envío
        # (sfc_completado=True) -- el item SIGUE pendiente hasta que el webhook al
        # CRM confirma recepción y marcar_exitoso lo saca de la cola de verdad. Dos
        # fases separadas del mismo cierre, no una sola.
        self.assertEqual(await self.queue_service.contar_pendientes(), 1)

        resultado_webhook_ok = await self.queue_service.marcar_exitoso(
            item.id, worker_id="worker_final", expected_version=claim_exitoso.version
        )
        self.assertEqual(resultado_webhook_ok, "completed")

        # --- 7. Estado final: consistente de punta a punta ---
        self.assertEqual(await self.queue_service.contar_pendientes(), 0)
        self.assertEqual(await self.redis.scard(f"{QUEUE_PREFIX}:status:EXITOSO"), 1)
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:claim:{item.id}"))

        raw_item_final = await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}")
        data_final = json.loads(raw_item_final)
        self.assertTrue(data_final["sfc_completado"])
        self.assertEqual(json.loads(data_final["sfc_response"]), {"codigo_queja": smart_code, "estado_cod": 2})

        idem_key_completado = IdempotencyService.construir_clave_completado(smart_code, payload)
        raw_idem_final = await self.redis.get(idem_key_completado)
        self.assertIsNotNone(raw_idem_final)
        idem_final = json.loads(raw_idem_final)
        self.assertEqual(idem_final["status"], "COMPLETED")
        self.assertEqual(idem_final["sfc_response"], {"codigo_queja": smart_code, "estado_cod": 2})


if __name__ == "__main__":
    unittest.main()
