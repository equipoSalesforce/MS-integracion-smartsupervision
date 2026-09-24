"""Explicit REOPEN metadata, with all external SFC traffic in MockTransport."""
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import get_s3_client, get_sfc_client
from app.core.config import settings
from app.integrations.sfc_client import SfcClient
from app.main import app
from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.services.idempotency_service import IdempotencyService
from app.services.momento_3_sync import Momento3SincronizacionService
from app.services.queue_service import ColaItemRedis, QueueService
from app.workers.scheduler import _ejecutar_paso_sfc, _ResultadoItemReintento
from tests.test_crm_payloads_validation import _payload_unificado_base


def payload(**overrides):
    return _payload_unificado_base(
        **dict(Smart_Code__c="128616551509974606", Status="In Progress",
               ClosedDate=None, marcacion__c="1", **overrides)
    )


class TestReopenContract(unittest.TestCase):
    def test_absent_marker_is_not_added_to_legacy_payload_or_hash(self):
        model = QuejaUnificadaCrmInput(**payload())
        normal = model.model_dump(by_alias=True, mode="json")
        self.assertNotIn("crm_operation", normal)
        explicit_none = QuejaUnificadaCrmInput(**payload(crm_operation=None))
        self.assertEqual(explicit_none.model_dump(mode="json"), normal)
        self.assertEqual(IdempotencyService.compute_payload_hash(normal),
                         IdempotencyService.compute_payload_hash(explicit_none.model_dump(mode="json")))

    def test_only_exact_reopen_literal_is_accepted(self):
        for operation in ("UPDATE", "CLOSE", "reopen", "", "REOPEN "):
            with self.subTest(operation=operation), self.assertRaises(ValidationError):
                QuejaUnificadaCrmInput(**payload(crm_operation=operation))
        model = QuejaUnificadaCrmInput(**payload(crm_operation="REOPEN"))
        self.assertEqual(model.model_dump(mode="json")["crm_operation"], "REOPEN")


class TestReopenTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        def capture(request):
            self.requests.append(request)
            return httpx.Response(200, json={"status": "updated"})

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(capture))
        self.sfc = SfcClient(interceptor=None, http_client=self.http)
        self.sfc.base_url = "https://sfc.invalid"
        self.service = Momento3SincronizacionService(self.sfc, s3_client=MagicMock())

    async def asyncTearDown(self):
        await self.http.aclose()

    def assert_request(self, reopen):
        request = self.requests[-1]
        self.assertEqual(request.method, "PATCH")
        body = json.loads(request.content)
        self.assertNotIn("crm_operation", body)
        self.assertEqual(body["codigo_queja"], payload()["Smart_Code__c"])
        self.assertFalse(body["anexo_queja"])
        self.assertFalse(body["documentacion_rta_final"])
        self.assertEqual(body["estado_cod"], 2)
        self.assertEqual(body["marcacion"], 1)
        self.assertNotIn("a_favor_de", body)
        if reopen:
            self.assertIn("fecha_cierre", body)
            self.assertIsNone(body["fecha_cierre"])
            self.assertIn(b'"fecha_cierre":null', request.content)
        else:
            self.assertNotIn("fecha_cierre", body)

    async def test_reopen_real_mapper_dto_and_http_serialization(self):
        await self.service.ejecutar_actualizacion_tramite(
            QuejaUnificadaCrmInput(**payload(crm_operation="REOPEN")))
        self.assert_request(reopen=True)

    async def test_normal_update_same_values_never_infers_reopen(self):
        await self.service.ejecutar_actualizacion_tramite(QuejaUnificadaCrmInput(**payload()))
        self.assert_request(reopen=False)

    async def test_old_queue_message_and_normal_retry_remain_compatible(self):
        raw = QuejaUnificadaCrmInput(**payload()).model_dump(mode="json")
        for serialized in (raw, json.dumps(raw)):
            item = ColaItemRedis({"id": 1, "smart_code": raw["Smart_Code__c"], "payload_json": serialized})
            orchestrator = DespachoQuejaOrquestador(self.sfc, s3_client=MagicMock(), m3_service=self.service)
            await orchestrator.procesar_despacho_raw_json(item.payload_json)
            self.assert_request(reopen=False)

    @unittest.skipUnless(os.getenv("TEST_REDIS_URL"), "Requires isolated local TEST_REDIS_URL")
    async def test_reopen_survives_real_redis_lua_round_trip(self):
        import redis.asyncio as redis_asyncio
        from urllib.parse import urlparse
        url = os.environ["TEST_REDIS_URL"]
        self.assertIn(urlparse(url).hostname, ("127.0.0.1", "localhost", "::1"))
        redis = redis_asyncio.from_url(url, decode_responses=True)
        try:
            queue = QueueService(redis_client=redis)
            raw = QuejaUnificadaCrmInput(**payload(crm_operation="REOPEN")).model_dump(mode="json")
            with patch("app.services.queue_service.EmailAlertService.notificar_falla_infraestructura", new_callable=AsyncMock):
                queued = await queue.encolar_despacho(raw["Smart_Code__c"], "AUTO", raw, "synthetic outage")
            item = await queue.reclamar_item_para_procesamiento(queued.id, "reopen-test", lease_segundos=60)
            self.assertEqual(item.payload_json, raw)
            self.assertEqual(item.payload_json["crm_operation"], "REOPEN")
            orchestrator = DespachoQuejaOrquestador(self.sfc, s3_client=MagicMock(), m3_service=self.service)
            proceed, _ = await _ejecutar_paso_sfc(
                orchestrator, queue, item, "reopen-test", item.payload_json, _ResultadoItemReintento())
            self.assertTrue(proceed)
            self.assert_request(reopen=True)
        finally:
            await redis.aclose()

    async def test_api_queue_worker_preserve_reopen_without_forwarding_it_to_sfc(self):
        # Use the real QueueService encoding boundary, with an in-memory Redis
        # eval double. Existing Redis integration tests cover the unchanged Lua.
        persisted = []

        async def eval_queue(script, numkeys, *values):
            args = values[numkeys:]
            item = {"id": 1, "smart_code": args[0], "payload_json": args[2],
                    "version": 1, "payload_hash": args[11]}
            persisted.append(json.dumps(item))
            return json.dumps({"data": item, "is_new": True, "pendientes_previos": 1})

        redis = AsyncMock()
        redis.eval.side_effect = eval_queue
        queue = QueueService(redis_client=redis)
        idem = MagicMock()
        idem.verificar_o_iniciar_operacion = AsyncMock(return_value=(False, None))
        idem.registrar_encolado = AsyncMock()
        lock = MagicMock()
        lock.acquire = AsyncMock(return_value=False)
        lock.release = AsyncMock()
        previous_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_sfc_client] = lambda: self.sfc
        app.dependency_overrides[get_s3_client] = lambda: MagicMock()
        try:
            with patch("app.api.routes_quejas.get_redis_client", return_value=redis), \
                 patch("app.api.routes_quejas.IdempotencyService", return_value=idem), \
                 patch("app.api.routes_quejas.RedisLock", return_value=lock), \
                 patch("app.api.routes_quejas.QueueService", return_value=queue):
                client = TestClient(app)
                response = client.post("/api/v1/quejas/sync/despacho",
                    json=payload(crm_operation="REOPEN"), headers={"X-API-Key": settings.CRM_API_KEY})
            self.assertEqual(response.status_code, 202, response.text)
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)
        raw = idem.verificar_o_iniciar_operacion.call_args.kwargs["payload_dict"]
        self.assertEqual(raw["crm_operation"], "REOPEN")
        item = ColaItemRedis(json.loads(persisted[0]))
        self.assertEqual(item.payload_json["crm_operation"], "REOPEN")
        durable = AsyncMock()
        durable.marcar_sfc_completado.return_value = "completed"
        orchestrator = DespachoQuejaOrquestador(self.sfc, s3_client=MagicMock(), m3_service=self.service)
        proceed, result = await _ejecutar_paso_sfc(
            orchestrator, durable, item, "synthetic-worker", item.payload_json, _ResultadoItemReintento())
        self.assertTrue(proceed)
        self.assertEqual(result["status"], "success")
        self.assert_request(reopen=True)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(durable.marcar_sfc_completado.call_args.kwargs["payload_dict"]["crm_operation"], "REOPEN")
        item.sfc_completado = True
        item.sfc_response = result
        await _ejecutar_paso_sfc(orchestrator, durable, item, "synthetic-worker", item.payload_json, _ResultadoItemReintento())
        self.assertEqual(len(self.requests), 1, "Durably completed retry must not repeat the SFC request")


if __name__ == "__main__":
    unittest.main()
