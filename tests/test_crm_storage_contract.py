import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.core.config import settings
from app.core.dispatch_observability import CrmDispatchEvidenceMiddleware, capture_prepared, capture_sfc_response, enable_crm_evidence
from app.core.exceptions import SfcIntegrationException
from app.core.mapping import SfcSalesforceMapper
from app.core.security.sanitizer import sanitizar_payload
from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.services.crm_storage_contract import normalize_crm_storage, normalize_reference
from app.services.s3_service import S3StorageService
from tests.test_crm_payloads_validation import _payload_unificado_base

CASE = "027ab011-052c-409d-aa7f-976b64b8251c"
OTHER = "027ab011-052c-409d-aa7f-976b64b8251d"
BUCKET = "synthetic-crm-files"
PREFIX = f"caso/{CASE}/adjuntos/"
URL = f"https://{BUCKET}.s3.us-east-1.amazonaws.com/"


def file(name="a", key=None, bucket=BUCKET):
    return {"nombre_archivo": name + ".pdf", "s3_key": key or PREFIX + name + "/file.pdf", "bucket": bucket}


class StorageContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bucket = patch.object(settings, "CRM_S3_BUCKET", BUCKET)
        self.bucket.start()
        self.addCleanup(self.bucket.stop)

    def test_zero_one_multiple_and_https(self):
        self.assertEqual(normalize_crm_storage(CASE, None, []), (None, []))
        for files in [[file()], [file(), file("b"), file("c")]]:
            directory, normalized = normalize_crm_storage(CASE, URL + PREFIX, files)
            self.assertEqual(directory, PREFIX)
            self.assertEqual(normalized, files)
        self.assertEqual(normalize_reference(URL + file()["s3_key"], CASE), (BUCKET, file()["s3_key"]))

    def test_invalid_references_fail_closed(self):
        bad = [
            (CASE, None, [file(key=file()["s3_key"].replace(CASE, OTHER))]),
            (CASE, URL + PREFIX.replace(CASE, OTHER), [file()]),
            (CASE, None, [file(), file("b", key=file()["s3_key"].replace(CASE, OTHER))]),
            (CASE, None, [file(bucket="other-bucket")]),
            (CASE, None, [file(key=PREFIX + "../other.pdf")]),
            (CASE, URL + PREFIX + "?X-Amz-Signature=secret", [file()]),
            ("2968", None, []), ("", None, []),
            (CASE, PREFIX + "a/", [file(), file("b")]),
            (CASE, None, [file(key=URL.replace(BUCKET, "other-bucket") + file()["s3_key"])]),
            (CASE, None, [file(key=URL + PREFIX + "%252e%252e/file.pdf")]),
            (CASE, None, [file(key=URL + PREFIX + "%2e%2e/file.pdf")]),
            (CASE, URL + PREFIX + "#fragment", []),
            (CASE, URL.replace("https:", "http:") + PREFIX, []),
            (CASE, URL.replace(".s3.", ".s3.evil.") + PREFIX, []),
            (CASE, None, [file(key=PREFIX + "a\\file.pdf")]),
        ]
        for owner, directory, files in bad:
            with self.subTest(owner=owner, directory=directory, files=files):
                with self.assertRaises(SfcIntegrationException) as caught:
                    normalize_crm_storage(owner, directory, files)
                self.assertEqual(caught.exception.error_type, "S3_KEY_OWNERSHIP_MISMATCH")

    def test_schema_preserves_legacy_serialization_and_sfc_mapping(self):
        source = _payload_unificado_base(Case_id="2968")
        legacy = QuejaUnificadaCrmInput(**source)
        crm = QuejaUnificadaCrmInput(**source, crm_case_uuid=CASE)
        self.assertNotIn("crm_case_uuid", legacy.model_dump())
        self.assertEqual(crm.Case_id, legacy.Case_id)
        self.assertEqual(QuejaUnificadaCrmInput.model_validate_json(crm.model_dump_json()).crm_case_uuid, CASE)
        for moment in (2, 3):
            expected = SfcSalesforceMapper.crm_entity_to_sfc_payload(legacy.model_dump(), momento=moment)
            actual = SfcSalesforceMapper.crm_entity_to_sfc_payload(crm.model_dump(), momento=moment)
            self.assertEqual(actual, expected)
            self.assertNotIn("crm_case_uuid", actual)
        self.assertNotEqual(sanitizar_payload({"crm_case_uuid": CASE})["crm_case_uuid"], CASE)

    async def test_batch_rejects_foreign_file_before_redis_or_s3(self):
        service = S3StorageService(s3_client=MagicMock())
        with patch("app.services.s3_service.get_redis_client") as redis:
            with self.assertRaises(SfcIntegrationException):
                await service.transferir_lote_s3_a_sfc(MagicMock(), "2968", [file(), file(key=PREFIX.replace(CASE, OTHER) + "b.pdf")], case_id="2968", crm_case_uuid=CASE)
            redis.assert_not_called()
            service.s3_client.assert_not_called()

    async def test_stream_uses_configured_crm_bucket_and_uuid_not_case_number(self):
        service = S3StorageService(s3_client=MagicMock())
        with patch.object(service, "_obtener_metadata_o_fallar", new=AsyncMock(return_value={"ContentLength": 1})) as head, patch.object(service, "_descargar_a_tmp_file", new=AsyncMock()), patch.object(service, "validar_integridad_archivo"):
            stream = await service.obtener_stream_archivo(URL + file()["s3_key"], bucket=BUCKET, case_id_esperado="2968", crm_case_uuid=CASE)
            stream.close()
            self.assertEqual(head.call_args.args[:2], (BUCKET, file()["s3_key"]))

    async def test_directory_lists_all_and_checks_returned_keys(self):
        boto = MagicMock()
        boto.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": f["s3_key"]} for f in [file(), file("b")]]}]
        service = S3StorageService(s3_client=boto)
        result = await service.listar_archivos_en_directorio(URL + PREFIX, case_id_esperado="2968", crm_case_uuid=CASE)
        self.assertEqual(len(result), 2)
        boto.get_paginator.return_value.paginate.assert_called_once_with(Bucket=BUCKET, Prefix=PREFIX)
        boto.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": PREFIX.replace(CASE, OTHER) + "file.pdf"}]}]
        with self.assertRaises(SfcIntegrationException):
            await service.listar_archivos_en_directorio(URL + PREFIX, crm_case_uuid=CASE)

    async def test_evidence_is_request_local_and_legacy_body_unchanged(self):
        async def app(scope, receive, send):
            if scope["crm"]:
                enable_crm_evidence()
                capture_prepared({"codigo_queja": scope["id"], "crm_case_uuid": CASE}, "SFC_CREATE_REQUEST")
                await capture_sfc_response(httpx.Response(400, json={"error_type": "REAL_TEST_RESPONSE", "secret": "hidden"}, request=httpx.Request("POST", "https://synthetic.invalid/queja")))
            await asyncio.sleep(0)
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b'{"status":"error"}'})
        middleware = CrmDispatchEvidenceMiddleware(app)
        async def call(identifier, crm):
            messages = []
            async def send(message): messages.append(message)
            await middleware({"type": "http", "path": "/api/v1/quejas/sync/despacho", "id": identifier, "crm": crm}, AsyncMock(), send)
            return json.loads(messages[-1]["body"])
        first, second, legacy = await asyncio.gather(call("first", True), call("second", True), call("legacy", False))
        self.assertEqual(legacy, {"status": "error"})
        for result, identifier in [(first, "first"), (second, "second")]:
            evidence = result["ssv_observability"]
            self.assertEqual(evidence["prepared"][0]["payload"]["codigo_queja"], identifier)
            self.assertNotIn("hidden", json.dumps(evidence))
            self.assertEqual(evidence["responses"][0]["http_status"], 400)
