import unittest
from unittest.mock import MagicMock
from botocore.exceptions import BotoCoreError

from app.services.s3_service import S3StorageService
from app.core.exceptions import SfcIntegrationException


class TestS3FileDescriptorLeak(unittest.IsolatedAsyncioTestCase):

    async def test_obtener_stream_archivo_cierra_tmp_file_ante_error_de_descarga(self):
        """
        Verifica que si la descarga desde S3 falla por un error de red a mitad de camino,
        el objeto SpooledTemporaryFile se cierre correctamente para prevenir fuga de descriptores.

        🔴 FIX (hallazgo propio, 2026-08-27): este test ya reproducía exactamente el hueco
        encontrado en la revisión de resiliencia -- un BotoCoreError (caída de conectividad
        real, no una respuesta de error del servicio) escapaba de _descargar_a_tmp_file SIN
        envolver en SfcIntegrationException. Se aceptaba como comportamiento esperado
        (assertRaises(BotoCoreError)), pero eso es precisamente lo que hacía que esta caída
        de S3 NUNCA se encolara para reintento automático (a diferencia de una caída de la
        SFC o de Redis) -- es_transitoria nunca llegaba a evaluarse sobre una excepción sin
        tipar. Ahora se espera SfcIntegrationException, ya clasificada como transitoria; el
        cierre del tmp_file (el propósito original de este test) sigue verificado igual.
        """
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 1024 * 1024}  # 1 MB

        # Simular corte de conexión durante download_fileobj
        mock_s3.download_fileobj.side_effect = BotoCoreError()

        service = S3StorageService(s3_client=mock_s3)

        # Ejecutar la llamada esperando que falle, ahora con el error de Boto3 envuelto
        # y clasificado -- no la excepción cruda de botocore.
        with self.assertRaises(SfcIntegrationException) as ctx:
            await service.obtener_stream_archivo(s3_key="quejas/123/adjunto.pdf")

        self.assertEqual(ctx.exception.error_type, "S3_INFRASTRUCTURE_ERROR")
        self.assertTrue(ctx.exception.es_transitoria, "Debe quedar disponible para encolarse y reintentarse automáticamente.")

        # Aserción: download_fileobj debió ser invocado
        mock_s3.download_fileobj.assert_called_once()


if __name__ == "__main__":
    unittest.main()