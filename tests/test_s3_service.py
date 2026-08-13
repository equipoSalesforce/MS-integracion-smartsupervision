# tests/test_s3_service.py
import unittest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError
from app.services.s3_service import S3StorageService
from app.core.exceptions import SfcIntegrationException


class TestS3ServiceErrorHandling(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_boto_client = MagicMock()
        self.service = S3StorageService(s3_client=self.mock_boto_client)

    async def test_listar_archivos_access_denied_raises_exception(self):
        """
        Verifica que si S3 responde con AccessDenied (403) al listar un directorio,
        NO devuelva lista vacía [] sino que eleve un SfcIntegrationException 500.
        """
        client_error = ClientError(
            error_response={"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            operation_name="ListObjectsV2"
        )
        paginator_mock = MagicMock()
        paginator_mock.paginate.side_effect = client_error
        self.mock_boto_client.get_paginator.return_value = paginator_mock

        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.service.listar_archivos_en_directorio(prefix="quejas/123")

        exc = ctx.exception
        self.assertEqual(exc.status_code, 500)
        self.assertEqual(exc.error_type, "S3_LIST_ERROR")

    async def test_obtener_stream_access_denied_raises_exception(self):
        """
        Verifica que AccessDenied en head_object lance S3_ACCESS_DENIED (500)
        en lugar de S3_FILE_NOT_FOUND (404).
        """
        client_error = ClientError(
            error_response={"Error": {"Code": "AccessDenied", "Message": "Forbidden"}},
            operation_name="HeadObject"
        )
        self.mock_boto_client.head_object.side_effect = client_error

        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.service.obtener_stream_archivo(s3_key="quejas/123/doc.pdf")

        exc = ctx.exception
        self.assertEqual(exc.status_code, 500)
        self.assertEqual(exc.error_type, "S3_ACCESS_DENIED")


if __name__ == "__main__":
    unittest.main()