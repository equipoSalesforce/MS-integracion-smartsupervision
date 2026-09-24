"""Post-QA mapping/CLOSE contracts. No real network or customer fixtures."""
import copy
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from app.core.mapping import SfcSalesforceMapper as Mapper
from app.services.momento_3_sync import Momento3SincronizacionService
from app.services.s3_service import S3StorageService
from app.services.final_response_contract import order_close_attachments, closure_identity
from app.core.exceptions import SfcIntegrationException


class MappingTests(unittest.TestCase):
    def test_partial_dynamic_map_keeps_replica_and_override_wins(self):
        with patch.object(Mapper,'MAPPING_MOMENTO_1_SFC_TO_CRM',{'correo':'EmailOverride'}):
            result=Mapper.sfc_payload_to_db_dict({'replica':1,'correo':'synthetic@example.invalid'})
            self.assertEqual(result['replica__c'],'Si')
            self.assertEqual(result['EmailOverride'],'synthetic@example.invalid')
        with patch.object(Mapper,'MAPPING_MOMENTO_1_SFC_TO_CRM',{'replica':'CustomReplica'}):
            result=Mapper.sfc_payload_to_db_dict({'replica':2})
            self.assertEqual(result['CustomReplica'],'No')
            self.assertNotIn('replica__c',result)

    def test_product_dynamic_label_local_fallback_unknown_and_missing(self):
        with patch.object(Mapper,'CATALOGOS',{'producto':{'9':'Tarjeta física'}}):
            for name,expected in [('TARJETA FISICA','Tarjeta física'),('wallet','Wallet'),
                                  ('Novel product','Novel product'),('', 'Cuenta perfil'),(None,'Cuenta perfil')]:
                self.assertEqual(Mapper.sfc_payload_to_db_dict({'producto_cod':207,'producto_nombre':name})['Product__c'],expected)


class CloseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.records={}
        self.redis=MagicMock()
        self.redis.get=AsyncMock(side_effect=lambda key:self.records.get(key))
        self.redis.hkeys=AsyncMock(return_value=[])
        async def save(key,value,**kwargs): self.records[key]=value
        self.redis.set=AsyncMock(side_effect=save)
        self.redis_patch=patch('app.services.momento_3_sync.get_redis_client',return_value=self.redis)
        self.redis_patch.start(); self.addCleanup(self.redis_patch.stop)
        self.client=MagicMock(); self.client.put_actualizar_queja=AsyncMock()
        self.service=Momento3SincronizacionService(self.client,s3_client=MagicMock())
        self.sent=[]
        async def transfer(**kwargs):
            self.sent.extend(a['nombre_archivo'] for a in kwargs['adjuntos_crm'])
            return [{'status':'success'}]
        self.service.s3_service.transferir_lote_s3_a_sfc=AsyncMock(side_effect=transfer)
        self.objects={}
        async def upload(**kwargs): self.objects[kwargs['s3_key']]=kwargs['file_bytes']
        async def download(**kwargs):
            if kwargs['s3_key'] not in self.objects:
                raise SfcIntegrationException(404,'S3_FILE_NOT_FOUND',None,'synthetic','synthetic')
            return io.BytesIO(self.objects[kwargs['s3_key']])
        self.service.s3_service.subir_bytes_archivo=AsyncMock(side_effect=upload)
        self.service.s3_service.obtener_stream_archivo=AsyncMock(side_effect=download)
        self.mapper_patch=patch.object(Mapper,'crm_entity_to_sfc_payload',side_effect=lambda *a,**k:{
            'codigo_queja':'synthetic','estado_cod':4,'canal_cod':13,'producto_cod':207,'macro_motivo_cod':940})
        self.mapper_patch.start(); self.addCleanup(self.mapper_patch.stop)
        self.pdf_patch=patch('app.services.momento_3_sync.generar_pdf_respuesta_final',return_value=b'%PDF-synthetic')
        self.pdf=self.pdf_patch.start(); self.addCleanup(self.pdf_patch.stop)
        self.request={'Case_id':'synthetic','Smart_Code__c':'synthetic','Status':'Closed','ClosedDate':'2026-09-24T01:00:00',
                      'cuerpo_respuesta_final':'Synthetic final answer','archivos_s3':[]}

    def file(self,name): return {'nombre_archivo':name,'s3_key':'caso/synthetic/'+name}

    async def close(self):
        return await self.service._orquestar_pipeline_momento_3(copy.deepcopy(self.request),generar_pdf_cierre=True)

    async def test_existing_final_last_no_generator_retry_no_final_resend(self):
        self.request['archivos_s3']=[self.file('RESP_FINAL_SFC.pdf'),self.file('B.pdf'),self.file('A.pdf')]
        await self.close()
        self.assertEqual(self.sent,['A.pdf','B.pdf','RESP_FINAL_SFC.pdf'])
        self.pdf.assert_not_called()
        self.assertTrue(self.service.s3_service.transferir_lote_s3_a_sfc.call_args.kwargs['ordered'])
        self.sent=[]
        await self.close()
        self.assertEqual(self.sent,['A.pdf','B.pdf'])
        self.pdf.assert_not_called()

    async def test_generated_final_last_and_not_regenerated_after_update_failure(self):
        self.request['archivos_s3']=[self.file('B.pdf'),self.file('A.pdf')]
        self.client.put_actualizar_queja.side_effect=[ConnectionError('synthetic'),None]
        with self.assertRaises(ConnectionError): await self.close()
        self.assertEqual(self.sent,['A.pdf','B.pdf','Respuesta_Final_synthetic_RESP_FINAL_SFC.pdf'])
        await self.close()
        self.pdf.assert_called_once()
        self.assertEqual(self.sent.count('Respuesta_Final_synthetic_RESP_FINAL_SFC.pdf'),1)

    async def test_generated_but_unsent_reuses_persisted_pdf(self):
        self.service.s3_service.transferir_lote_s3_a_sfc.side_effect=[ConnectionError('synthetic'),[]]
        with self.assertRaises(ConnectionError): await self.close()
        await self.close()
        self.pdf.assert_called_once()
        self.assertEqual(self.service.s3_service.obtener_stream_archivo.await_count,2)

    async def test_existing_object_without_receipt_is_reused_not_regenerated(self):
        key=f"caso/{closure_identity(self.request)}/synthetic/Respuesta_Final_synthetic_RESP_FINAL_SFC.pdf"
        self.objects[key]=b'%PDF-synthetic'
        await self.close()
        self.pdf.assert_not_called()
        self.service.s3_service.subir_bytes_archivo.assert_not_awaited()

    async def test_legacy_accepted_pdf_checkpoint_suppresses_generation(self):
        self.redis.hkeys.return_value=['caso/synthetic/Respuesta_Final_synthetic_RESP_FINAL_SFC.pdf:originalhash']
        await self.close()
        self.pdf.assert_not_called()
        self.service.s3_service.transferir_lote_s3_a_sfc.assert_not_awaited()

    async def test_normal_attachment_failure_never_sends_final(self):
        self.request['archivos_s3']=[self.file('A.pdf')]
        self.service.s3_service.transferir_lote_s3_a_sfc.side_effect=ConnectionError('synthetic')
        with self.assertRaises(ConnectionError): await self.close()
        self.pdf.assert_not_called()
        self.client.put_actualizar_queja.assert_not_awaited()

    async def test_storage_order_is_sequential_and_stops_at_error(self):
        storage=S3StorageService(s3_client=MagicMock())
        sequence=[]
        async def send(item,ctx):
            sequence.append(item['nombre_archivo'])
            if item['nombre_archivo']=='B.pdf': raise ConnectionError('synthetic')
            return {}
        storage._procesar_envio_s3_a_sfc=AsyncMock(side_effect=send)
        with patch('app.services.s3_service.get_redis_client',return_value=None):
            with self.assertRaises(ConnectionError):
                await storage.transferir_lote_s3_a_sfc(self.client,'synthetic',order_close_attachments(
                    [self.file('RESP_FINAL_SFC.pdf'),self.file('B.pdf'),self.file('A.pdf')]),ordered=True)
        self.assertEqual(sequence,['A.pdf','B.pdf'])

    def test_closure_identity_stable_and_scoped_to_cycle(self):
        self.assertEqual(closure_identity(self.request),closure_identity({**self.request,'cuerpo_respuesta_final':'changed'}))
        self.assertNotEqual(closure_identity(self.request),closure_identity({**self.request,'ClosedDate':'2026-09-25'}))
