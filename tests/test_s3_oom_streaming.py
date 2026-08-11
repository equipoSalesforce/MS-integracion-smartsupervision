import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock
from app.integrations.sfc_client import SfcClient

class TestS3OOMStreaming(unittest.IsolatedAsyncioTestCase):

    async def test_post_adjunto_queja_mantiene_stream_sin_convertir_a_bytes(self):
        """
        Verifica que post_adjunto_queja reciba un SpooledTemporaryFile y se lo pase
        directamente a httpx en el diccionario 'files' sin invocar .read() globalmente en RAM.
        """
        mock_interceptor = MagicMock()
        mock_interceptor.get_valid_token = AsyncMock(return_value="mock_jwt_token")
        mock_interceptor.signature_context.get_signature.return_value = "MOCK_SIGNATURE"

        mock_http_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "success", "id": 123}
        mock_http_client.post = AsyncMock(return_value=mock_response)

        sfc_client = SfcClient(interceptor=mock_interceptor, http_client=mock_http_client)

        # 1. Crear un SpooledTemporaryFile con contenido de prueba
        tmp_file = tempfile.SpooledTemporaryFile(max_size=5 * 1024 * 1024)
        contenido_prueba = b"%PDF-1.4 Contenido de prueba pesado para streaming sin OOM"
        tmp_file.write(contenido_prueba)
        tmp_file.seek(0)

        # 2. Ejecutar la llamada a post_adjunto_queja
        resultado = await sfc_client.post_adjunto_queja(
            sfc_codigo_queja="1286TEST001",
            file_data=tmp_file,
            file_type="pdf",
            file_name="soporte_pesado.pdf"
        )

        # 3. Aserciones
        self.assertEqual(resultado["id"], 123)
        mock_http_client.post.assert_called_once()

        # Verificar el parámetro 'files' enviado a httpx.post
        call_kwargs = mock_http_client.post.call_args[1]
        files_sent = call_kwargs.get("files")
        
        self.assertIn("file", files_sent)
        file_tuple = files_sent["file"]
        
        # file_tuple[1] debe ser la misma instancia de SpooledTemporaryFile, NO una instancia de bytes
        objeto_archivo_enviado = file_tuple[1]
        self.assertNotIsInstance(
            objeto_archivo_enviado, 
            bytes, 
            "El archivo fue convertido a bytes en RAM, rompiendo el streaming anti-OOM."
        )
        self.assertTrue(
            hasattr(objeto_archivo_enviado, "read"), 
            "El objeto enviado a httpx debe conservar el método .read() para streaming."
        )

        tmp_file.close()


if __name__ == "__main__":
    unittest.main()