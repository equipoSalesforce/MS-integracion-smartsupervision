# tests/test_s3_upload_y_ssrf.py
"""
Cobertura de S3StorageService.subir_stream_archivo / subir_bytes_archivo (antes
sin tests directos) y _es_host_permitido_sfc (la validación SSRF para adjuntos
descargados desde la SFC). Mismo patrón de fixtures que tests/test_s3_service.py
(MagicMock del cliente boto3 -- se invoca vía asyncio.to_thread, así que un mock
sincrónico normal es suficiente).
"""
import io
import unittest
from unittest.mock import MagicMock, patch

from app.services.s3_service import S3StorageService
from app.core.exceptions import SfcIntegrationException
from app.core.config import settings


class TestSubirStreamArchivo(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_boto_client = MagicMock()
        self.service = S3StorageService(s3_client=self.mock_boto_client)

    async def test_sube_exitosamente_y_retorna_key_limpia(self):
        resultado = await self.service.subir_stream_archivo(
            s3_key="/caso/SC-1/doc.pdf", file_obj=io.BytesIO(b"%PDF-1.4 contenido")
        )

        self.assertEqual(resultado, "caso/SC-1/doc.pdf")
        self.mock_boto_client.upload_fileobj.assert_called_once()
        kwargs = self.mock_boto_client.upload_fileobj.call_args.kwargs
        self.assertEqual(kwargs["Key"], "caso/SC-1/doc.pdf")
        self.assertEqual(kwargs["ExtraArgs"], {"ContentType": "application/pdf"})

    async def test_error_de_boto3_se_traduce_a_sfc_integration_exception(self):
        self.mock_boto_client.upload_fileobj.side_effect = RuntimeError("bucket no existe")
        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.service.subir_stream_archivo(s3_key="caso/SC-1/doc.pdf", file_obj=io.BytesIO(b"x"))
        self.assertEqual(ctx.exception.error_type, "S3_UPLOAD_ERROR")

    async def test_sin_cliente_en_produccion_lanza_infraestructura(self):
        service = S3StorageService(s3_client=None)
        service.is_local = False
        with self.assertRaises(SfcIntegrationException) as ctx:
            await service.subir_stream_archivo(s3_key="caso/SC-1/doc.pdf", file_obj=io.BytesIO(b"x"))
        self.assertEqual(ctx.exception.error_type, "INFRASTRUCTURE_ERROR")

    async def test_sin_cliente_en_local_retorna_key_simulada(self):
        service = S3StorageService(s3_client=None)
        service.is_local = True
        resultado = await service.subir_stream_archivo(s3_key="caso/SC-1/doc.pdf", file_obj=io.BytesIO(b"x"))
        self.assertEqual(resultado, "caso/SC-1/doc.pdf")


class TestSubirBytesArchivo(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_boto_client = MagicMock()
        self.service = S3StorageService(s3_client=self.mock_boto_client)

    async def test_sube_exitosamente_via_put_object(self):
        resultado = await self.service.subir_bytes_archivo(s3_key="caso/SC-2/doc.pdf", file_bytes=b"%PDF-1.4")

        self.assertEqual(resultado, "caso/SC-2/doc.pdf")
        self.mock_boto_client.put_object.assert_called_once()
        kwargs = self.mock_boto_client.put_object.call_args.kwargs
        self.assertEqual(kwargs["Body"], b"%PDF-1.4")

    async def test_error_de_boto3_se_traduce_a_sfc_integration_exception(self):
        self.mock_boto_client.put_object.side_effect = RuntimeError("acceso denegado")
        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.service.subir_bytes_archivo(s3_key="caso/SC-2/doc.pdf", file_bytes=b"x")
        self.assertEqual(ctx.exception.error_type, "S3_UPLOAD_ERROR")

    async def test_sin_cliente_en_local_retorna_key_simulada(self):
        service = S3StorageService(s3_client=None)
        service.is_local = True
        resultado = await service.subir_bytes_archivo(s3_key="caso/SC-2/doc.pdf", file_bytes=b"x")
        self.assertEqual(resultado, "caso/SC-2/doc.pdf")


class TestEsHostPermitidoSfc(unittest.TestCase):

    def setUp(self):
        self.service = S3StorageService(s3_client=MagicMock())

    def test_url_vacia_no_permitida(self):
        self.assertFalse(self.service._es_host_permitido_sfc(""))
        self.assertFalse(self.service._es_host_permitido_sfc(None))

    def test_mismo_host_que_sfc_url_base_permitido(self):
        with patch.object(settings, "SFC_URL_BASE", "https://qasmart.superfinanciera.gov.co"):
            self.assertTrue(
                self.service._es_host_permitido_sfc("https://qasmart.superfinanciera.gov.co/api/storage/x.pdf")
            )

    def test_subdominio_de_superfinanciera_permitido(self):
        self.assertTrue(self.service._es_host_permitido_sfc("https://otro.superfinanciera.gov.co/x.pdf"))

    def test_storage_googleapis_permitido(self):
        self.assertTrue(self.service._es_host_permitido_sfc("https://storage.googleapis.com/bucket/x.pdf"))

    def test_host_arbitrario_no_permitido(self):
        self.assertFalse(self.service._es_host_permitido_sfc("https://evil.attacker.com/x.pdf"))

    def test_localhost_permitido_solo_si_es_local(self):
        self.service.is_local = True
        self.assertTrue(self.service._es_host_permitido_sfc("http://minio:9000/x.pdf"))

        self.service.is_local = False
        with patch.object(settings, "ENVIRONMENT", "production"):
            self.assertFalse(self.service._es_host_permitido_sfc("http://minio:9000/x.pdf"))


if __name__ == "__main__":
    unittest.main()
