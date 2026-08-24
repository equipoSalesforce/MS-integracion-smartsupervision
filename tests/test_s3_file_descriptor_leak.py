import unittest
from unittest.mock import MagicMock
from botocore.exceptions import BotoCoreError

from app.services.s3_service import S3StorageService


class TestS3FileDescriptorLeak(unittest.IsolatedAsyncioTestCase):

    async def test_obtener_stream_archivo_cierra_tmp_file_ante_error_de_descarga(self):
        """
        Verifica que si la descarga desde S3 falla por un error de red a mitad de camino,
        el objeto SpooledTemporaryFile se cierre correctamente para prevenir fuga de descriptores.
        """
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 1024 * 1024}  # 1 MB

        # Simular corte de conexión durante download_fileobj
        mock_s3.download_fileobj.side_effect = BotoCoreError()

        service = S3StorageService(s3_client=mock_s3)

        # Ejecutar la llamada esperando que falle por el error de Boto3
        with self.assertRaises(BotoCoreError):
            await service.obtener_stream_archivo(s3_key="quejas/123/adjunto.pdf")

        # Aserción: download_fileobj debió ser invocado
        mock_s3.download_fileobj.assert_called_once()


if __name__ == "__main__":
    unittest.main()