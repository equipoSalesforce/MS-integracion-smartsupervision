"""CLOSE/RECLOSE parity through real schema, PDF, storage transfer and receipts."""
import copy
import hashlib
import io
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from pypdf import PdfReader
from app.core.exceptions import SfcIntegrationException
from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.services.idempotency_service import IdempotencyService
from app.services.momento_3_sync import Momento3SincronizacionService
from app.utils.pdf_generator import generar_pdf_respuesta_final, vincular_pdf_a_ciclo
from tests.test_momento_3 import _StubRedisHash

CYCLE = '3a4bb42e-0dc5-4b99-a42d-49ceef8ad72d'
NEXT_CYCLE = '8c949135-1b96-45e3-8f23-a2f671a42494'


def form_values(content):
    return {name: (field.get('/V'), field.get('/Ff'))
            for name, field in PdfReader(io.BytesIO(content)).get_fields().items()}


class ReclosePipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = _StubRedisHash()
        for target in ('momento_3_sync', 's3_service'):
            context = patch(f'app.services.{target}.get_redis_client', return_value=self.redis)
            context.start()
            self.addCleanup(context.stop)
        context = patch('app.services.momento_3_sync.SfcSalesforceMapper.crm_entity_to_sfc_payload',
                        side_effect=lambda *a, **k: {'canal_cod': 13, 'producto_cod': 207, 'macro_motivo_cod': 940})
        context.start()
        self.addCleanup(context.stop)
        context = patch('app.services.momento_3_sync.generar_pdf_respuesta_final', wraps=generar_pdf_respuesta_final)
        self.generate = context.start()
        self.addCleanup(context.stop)
        self.events = []
        self.objects = {}
        self.client = MagicMock()
        self.client.post_adjunto_queja = AsyncMock(side_effect=self.upload)
        self.client.put_actualizar_queja = AsyncMock(side_effect=self.update)
        self.service = Momento3SincronizacionService(self.client, s3_client=MagicMock())
        self.service.s3_service.obtener_stream_archivo = AsyncMock(side_effect=self.download)
        self.service.s3_service.subir_bytes_archivo = AsyncMock(side_effect=self.persist)
        self.payload = {
            'Case_id': 'SYNTHETIC-3024', 'Smart_Code__c': '12861790300000000000', 'Status': 'Closed',
            'SuppliedName': 'Synthetic QA', 'SC_id_type__c': 'CC', 'id_number__c': '123456789',
            'tipo_de_persona__c': 'B2C', 'canal__c': 'Internet', 'punto_recepcion': 'Manual',
            'Instancia_de_recepcion__c': 'Entidad vigilada', 'Description': 'Synthetic parity test',
            'direccion__c': 'Synthetic test address',
            'Product__c': 'Cuenta perfil', 'Categorias_COL__c': 'Transacción no reconocida',
            'Departamento__c': 'Bogotá D.C.', 'SC_municipio__c': 'Bogotá D.C.',
            'ClosedDate': datetime.now(ZoneInfo('America/Bogota')).strftime('%Y-%m-%d'),
            'Favorabilidad__c': 'Favorable',
            'Aceptacion__c': 'Respuesta final a favor del consumidor financiero aceptadas por la entidad',
            'archivos_s3': [],
        }

    async def persist(self, *, s3_key, file_bytes, **kwargs):
        self.objects[s3_key] = file_bytes

    async def download(self, *, s3_key, **kwargs):
        if s3_key not in self.objects:
            raise SfcIntegrationException(404, 'S3_FILE_NOT_FOUND', None, 'Synthetic missing', 'Retry')
        return io.BytesIO(self.objects[s3_key])

    async def upload(self, *, file_name, file_data, **kwargs):
        self.events.append(('upload', file_name, file_data.read()))
        return {'id': 123}

    async def update(self, **kwargs):
        receipt = await IdempotencyService(self.redis).obtener_respuesta_final(CYCLE)
        if self.payload.get('crm_reopen_operation_id') == CYCLE:
            expected = self.service._final_receipt_identity(self.payload['Case_id'], self.payload['Smart_Code__c'], CYCLE, replica=True)
            self.assertTrue(self.service._confirmed_final_receipt(receipt, expected, replica=True))
        self.events.append(('patch', kwargs['payload']))
        return {'estado_cod': 4}

    def replica(self):
        self.payload.update(crm_operation='RECLOSE', crm_reopen_operation_id=CYCLE)

    def attachment(self, name):
        key = 'caso/SYNTHETIC-3024/' + name
        self.objects[key] = generar_pdf_respuesta_final('Synthetic', 'synthetic', 'Synthetic normal attachment')
        item = {'nombre_archivo': name, 's3_key': key, 'bucket': 'synthetic-bucket'}
        self.payload['archivos_s3'].append(item)
        return key

    async def close(self):
        model = QuejaUnificadaCrmInput.model_validate(copy.deepcopy(self.payload))
        return await self.service.ejecutar_cierre_definitivo(model)

    async def receipt(self):
        return await IdempotencyService(self.redis).obtener_respuesta_final(CYCLE)

    async def assert_blocked(self):
        with self.assertRaises(SfcIntegrationException) as error:
            await self.close()
        self.assertEqual(error.exception.error_type, 'FINAL_RESPONSE_DOCUMENT_NOT_SENT')
        self.assertEqual(error.exception.crm_action, 'No fue posible enviar la respuesta final de la réplica antes del cierre.')
        self.assertNotEqual(error.exception.status_code, 404)
        self.client.put_actualizar_queja.assert_not_awaited()

    async def test_a_b_default_body_parity_real_pdf_and_order(self):
        for replica in (False, True):
            for body in ('ABSENT', None, '', '   '):
                with self.subTest(replica=replica, body=body):
                    self.redis.values.clear()
                    self.redis.hashes.clear()
                    self.objects.clear()
                    self.events.clear()
                    self.generate.reset_mock()
                    self.payload['archivos_s3'] = []
                    for key in ('crm_operation', 'crm_reopen_operation_id', 'cuerpo_respuesta_final'):
                        self.payload.pop(key, None)
                    if replica:
                        self.replica()
                    if body != 'ABSENT':
                        self.payload['cuerpo_respuesta_final'] = body
                    self.attachment('B.pdf')
                    self.attachment('A.pdf')
                    await self.close()
                    names = [event[1] for event in self.events if event[0] == 'upload']
                    self.assertEqual(names[:2], ['A.pdf', 'B.pdf'])
                    self.assertIn('REPLICA_RESP_FINAL_SFC' if replica else '_RESP_FINAL_SFC', names[-1])
                    if not replica:
                        self.assertNotIn('REPLICA', names[-1])
                    self.assertEqual(self.events[-1][0], 'patch')
                    self.assertEqual(self.events[-1][1]['estado_cod'], 4)
                    self.assertIs(self.events[-1][1]['documentacion_rta_final'], True)
                    fields = PdfReader(io.BytesIO(self.events[-2][2])).get_fields()
                    self.assertEqual(fields['mensaje_cuerpo']['/V'],
                                     'Se emite respuesta formal y cierre definitivo al caso de reclamación.')
                    self.generate.assert_called_once()

    async def test_c_historical_document_never_reused_or_changed(self):
        self.replica()
        original = self.attachment('original_RESP_FINAL_SFC.pdf')
        old_replica = self.attachment('old_REPLICA_RESP_FINAL_SFC.pdf')
        before = dict(self.objects)
        await self.close()
        self.assertEqual(self.objects[original], before[original])
        self.assertEqual(self.objects[old_replica], before[old_replica])
        self.assertEqual(self.client.post_adjunto_queja.await_count, 1)
        self.assertNotIn(self.events[0][1], ('original_RESP_FINAL_SFC.pdf', 'old_REPLICA_RESP_FINAL_SFC.pdf'))

    async def test_d_upload_failure_retry_reuses_same_document(self):
        self.replica()
        self.client.post_adjunto_queja.side_effect = ConnectionError('Synthetic upload failure')
        await self.assert_blocked()
        original = dict(self.objects)
        self.assertFalse((await self.receipt())['sent'])
        self.client.post_adjunto_queja.side_effect = self.upload
        with patch('app.services.momento_3_sync.capture_prepared') as stage:
            await self.close()
        self.assertIn('REPLICA_RESP_FINAL_REUSED_CURRENT_CYCLE', [c.args[1] for c in stage.call_args_list])
        self.assertEqual(self.objects, original)
        self.generate.assert_called_once()

    async def test_d_patch_failure_retry_only_patch(self):
        self.replica()
        self.attachment('A.pdf')
        self.client.put_actualizar_queja.side_effect = ConnectionError('Synthetic patch failure')
        with self.assertRaises(ConnectionError):
            await self.close()
        self.assertTrue((await self.receipt())['sent'])
        self.client.put_actualizar_queja.side_effect = self.update
        await self.close()
        self.generate.assert_called_once()
        self.assertEqual(self.client.post_adjunto_queja.await_count, 2)

    async def test_e_future_cycle_same_default_distinct_document_bytes(self):
        self.replica()
        await self.close()
        self.payload['crm_reopen_operation_id'] = NEXT_CYCLE
        await self.close()
        uploads = [e for e in self.events if e[0] == 'upload']
        self.assertNotEqual(uploads[0][1], uploads[1][1])
        self.assertNotEqual(uploads[0][2], uploads[1][2])
        self.assertEqual(form_values(uploads[0][2]), form_values(uploads[1][2]))
        self.assertEqual(self.generate.call_count, 2)

    async def test_f_duplicate_rejection_is_not_acceptance_or_checkpoint(self):
        self.replica()
        self.client.post_adjunto_queja.side_effect = SfcIntegrationException(
            400, 'DUPLICATE_FILE', None, 'El documento ya existe', 'Synthetic')
        await self.assert_blocked()
        self.assertFalse((await self.receipt())['sent'])
        self.assertEqual(self.redis.hashes, {})

    async def test_g_accepted_file_receipt_survives_final_receipt_write_failure(self):
        self.replica()
        original_set = self.redis.set
        async def fail_sent(key, value, **kwargs):
            if json.loads(value).get('sent'):
                raise ConnectionError('Synthetic checkpoint outage')
            await original_set(key, value, **kwargs)
        self.redis.set = fail_sent
        await self.assert_blocked()
        self.redis.set = original_set
        await self.close()
        self.client.post_adjunto_queja.assert_awaited_once()
        self.generate.assert_called_once()

    async def test_h_foreign_cycle_receipt_never_satisfies_guard(self):
        self.replica()
        expected = self.service._final_receipt_identity(self.payload['Case_id'], self.payload['Smart_Code__c'], NEXT_CYCLE, replica=True)
        await IdempotencyService(self.redis).guardar_respuesta_final(CYCLE, {**expected, 'sent': True, 'version': 2})
        await self.assert_blocked()
        self.client.post_adjunto_queja.assert_not_awaited()

    async def test_h_legacy_false_sent_and_duplicate_file_checkpoint_are_repaired_without_regeneration(self):
        self.replica()
        expected = self.service._final_receipt_identity(self.payload['Case_id'], self.payload['Smart_Code__c'], CYCLE, replica=True)
        original = generar_pdf_respuesta_final('Synthetic', 'synthetic', 'Synthetic original default')
        self.objects[expected['s3_key']] = original
        checkpoints = IdempotencyService(self.redis)
        await checkpoints.guardar_respuesta_final(CYCLE, {'s3_key': expected['s3_key'], 'sent': True})
        await checkpoints.marcar_archivo_completado(self.payload['Smart_Code__c'],
            expected['s3_key'] + ':' + hashlib.sha256(original).hexdigest()[:16],
            {'file_name': expected['file_name'], 'status': 'DUPLICATE_OMITTED'})
        await self.close()
        self.generate.assert_not_called()
        self.client.post_adjunto_queja.assert_awaited_once()
        current = self.objects[expected['s3_key']]
        self.assertNotEqual(original, current)
        self.assertEqual(form_values(original), form_values(current))
        self.assertTrue((await self.receipt())['sent'])

    async def test_h_silent_checkpoint_write_loss_blocks_patch(self):
        self.replica()
        self.redis.set = AsyncMock()
        await self.assert_blocked()

    async def test_h_batch_without_confirmation_never_patches(self):
        self.replica()
        for value in ([], [{'file_name': 'other.pdf', 'status': 'OK'}],
                      [{'file_name': 'other.pdf', 'status': 'DUPLICATE_OMITTED'}]):
            self.service.s3_service.transferir_lote_s3_a_sfc = AsyncMock(return_value=value)
            await self.assert_blocked()

    async def test_missing_persisted_replica_does_not_regenerate_or_self_heal(self):
        self.replica()
        expected = self.service._final_receipt_identity(self.payload['Case_id'], self.payload['Smart_Code__c'], CYCLE, replica=True)
        await IdempotencyService(self.redis).guardar_respuesta_final(CYCLE, {**expected, 'sent': False})
        await self.assert_blocked()
        self.generate.assert_not_called()

    def test_pdf_binding_is_stable_and_does_not_change_template_or_body(self):
        original = generar_pdf_respuesta_final('Synthetic', 'synthetic', 'Synthetic default')
        bound = vincular_pdf_a_ciclo(original, CYCLE)
        self.assertNotEqual(original, bound)
        self.assertEqual(bound, vincular_pdf_a_ciclo(bound, CYCLE))
        self.assertEqual(form_values(original), form_values(bound))
        with self.assertRaises(ValueError):
            vincular_pdf_a_ciclo(bound, NEXT_CYCLE)
