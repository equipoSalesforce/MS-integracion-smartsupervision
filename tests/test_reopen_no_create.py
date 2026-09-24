import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from app.core.dispatch_observability import _evidence, capture_sfc_response
from app.core.exceptions import SfcIntegrationException
from app.core.reopen_diagnostic import reopen_diagnostic, capture_reopen_payload
from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from tests.test_reopen_operation import payload


class ReopenNoCreateTests(unittest.IsolatedAsyncioTestCase):
    async def test_reopen_success_and_all_errors_never_create_and_preserve_original(self):
        for status,code in [(200,None),(404,'NOT_FOUND_ERROR'),(400,'NOT_FOUND_ERROR'),(503,'SFC_UNAVAILABLE')]:
            m2,m3=MagicMock(),MagicMock()
            m2.ejecutar_envio_momento_2=AsyncMock()
            error=SfcIntegrationException(status,code,None,'original rejection','original action') if code else None
            m3.ejecutar_actualizacion_tramite=AsyncMock(side_effect=error,return_value={'status':'success'})
            orchestrator=DespachoQuejaOrquestador(MagicMock(),s3_client=MagicMock(),m2_service=m2,m3_service=m3)
            with patch('app.core.reopen_diagnostic.logger.info') as log:
                request=QuejaUnificadaCrmInput(**payload(crm_operation='REOPEN'))
                if error:
                    with self.assertRaises(SfcIntegrationException) as caught:
                        await orchestrator.procesar_despacho(request)
                    self.assertIs(caught.exception,error)
                else:
                    self.assertEqual(await orchestrator.procesar_despacho(request),{'status':'success'})
                evidence=log.call_args.kwargs['extra']['extra_data']
                self.assertEqual(evidence['create_fallback_suppressed'],bool(error))
                self.assertEqual(evidence['provider_error_code'],code)
            m2.ejecutar_envio_momento_2.assert_not_awaited()
            m3.ejecutar_actualizacion_tramite.assert_awaited_once()

    async def test_normal_update_keeps_existing_self_healing(self):
        m2,m3=MagicMock(),MagicMock()
        m2.ejecutar_envio_momento_2=AsyncMock(return_value={'status':'success'})
        original=SfcIntegrationException(404,'NOT_FOUND_ERROR',None,'original rejection','original action')
        m3.ejecutar_actualizacion_tramite=AsyncMock(side_effect=[original,{'status':'success'}])
        orchestrator=DespachoQuejaOrquestador(MagicMock(),s3_client=MagicMock(),m2_service=m2,m3_service=m3)
        self.assertEqual(await orchestrator.procesar_despacho(QuejaUnificadaCrmInput(**payload())),{'status':'success'})
        m2.ejecutar_envio_momento_2.assert_awaited_once()
        self.assertEqual(m3.ejecutar_actualizacion_tramite.await_count,2)

    async def test_safe_diagnostic_keeps_real_null_and_no_personal_values(self):
        evidence={'enabled':False,'prepared':[],'responses':[]}
        token=_evidence.set(evidence)
        try:
            with reopen_diagnostic('synthetic-code'):
                capture_reopen_payload({'anexo_queja':False,'documentacion_rta_final':False,'estado_cod':2,
                    'fecha_cierre':None,'marcacion':1,'nombres':'do-not-expose','numero_id_CF':'do-not-expose',
                    'email':'do-not-expose','authorization':'do-not-expose'})
                await capture_sfc_response(httpx.Response(200,json={},request=httpx.Request('PATCH','https://sfc.invalid')))
            diagnostic=evidence['reopen']
            self.assertTrue(diagnostic['update_attempted'])
            self.assertEqual(diagnostic['sfc_http_status'],200)
            self.assertEqual(diagnostic['contract_values'],{'anexo_queja':False,'documentacion_rta_final':False,
                'estado_cod':2,'fecha_cierre':None,'marcacion':1})
            serialized=json.dumps(diagnostic)
            self.assertIn('"fecha_cierre": null',serialized)
            self.assertNotIn('do-not-expose',serialized)
            self.assertNotIn('synthetic-code',serialized)
        finally:
            _evidence.reset(token)
