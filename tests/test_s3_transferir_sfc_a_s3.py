# tests/test_s3_transferir_sfc_a_s3.py
"""
Cobertura de S3StorageService.transferir_lote_sfc_a_s3 y su cadena de helpers
(_procesar_adjunto_sfc_a_s3, _descargar_adjunto_streaming,
_validar_url_sfc_permitida) -- el flujo de Momento 1 que descarga adjuntos
desde la SFC (streaming, límite 30MB) y los sube a S3. Antes sin cobertura
directa (0%).

httpx.MockTransport simula la descarga desde la SFC; el cliente boto3 se
mockea igual que el resto de la suite (MagicMock, se invoca vía
asyncio.to_thread).
"""
import unittest
from unittest.mock import MagicMock

import httpx

from app.services.s3_service import S3StorageService


def _service_con_transport(handler, s3_client=None, is_local=False) -> S3StorageService:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    service = S3StorageService(s3_client=s3_client or MagicMock(), http_client=http_client)
    service.is_local = is_local
    return service


class TestTransferirLoteSfcAS3(unittest.IsolatedAsyncioTestCase):

    async def test_lista_vacia_retorna_vacio_sin_tocar_red(self):
        service = S3StorageService(s3_client=MagicMock())
        resultado = await service.transferir_lote_sfc_a_s3(codigo_queja="SC-1", adjuntos_sfc=[])
        self.assertEqual(resultado, [])

    async def test_descarga_y_sube_exitosamente(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"%PDF-1.4 contenido de prueba")

        service = _service_con_transport(handler)
        resultado = await service.transferir_lote_sfc_a_s3(
            codigo_queja="SC-1",
            adjuntos_sfc=[{"file": "https://qasmart.superfinanciera.gov.co/api/storage/x.pdf", "id": "doc1", "type": "application/pdf"}]
        )

        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0]["nombre_archivo"], "doc1.pdf")
        self.assertEqual(resultado[0]["s3_key"], "quejas/SC-1/doc1.pdf")
        service.s3_client.upload_fileobj.assert_called_once()

    async def test_url_no_permitida_ssrf_se_omite_del_resultado(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"%PDF-1.4 no deberia llegar aqui")

        service = _service_con_transport(handler)
        resultado = await service.transferir_lote_sfc_a_s3(
            codigo_queja="SC-1",
            adjuntos_sfc=[{"file": "https://evil.attacker.com/x.pdf", "id": "doc1", "type": "application/pdf"}]
        )

        # asyncio.gather(return_exceptions=True) absorbe la excepción -- no propaga,
        # pero el adjunto no aparece en el resultado final.
        self.assertEqual(resultado, [])
        service.s3_client.upload_fileobj.assert_not_called()

    async def test_fallo_de_descarga_en_produccion_se_omite(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        service = _service_con_transport(handler, is_local=False)
        resultado = await service.transferir_lote_sfc_a_s3(
            codigo_queja="SC-1",
            adjuntos_sfc=[{"file": "https://qasmart.superfinanciera.gov.co/api/storage/x.pdf", "id": "doc1", "type": "application/pdf"}]
        )

        self.assertEqual(resultado, [])

    async def test_fallo_de_descarga_en_local_retorna_metadata_simulada(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        service = _service_con_transport(handler, is_local=True)
        resultado = await service.transferir_lote_sfc_a_s3(
            codigo_queja="SC-1",
            adjuntos_sfc=[{"file": "https://qasmart.superfinanciera.gov.co/api/storage/x.pdf", "id": "doc1", "type": "application/pdf"}]
        )

        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0]["nombre_archivo"], "doc1.pdf")

    async def test_procesa_varios_adjuntos_en_paralelo(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"%PDF-1.4 contenido")

        service = _service_con_transport(handler)
        resultado = await service.transferir_lote_sfc_a_s3(
            codigo_queja="SC-1",
            adjuntos_sfc=[
                {"file": "https://qasmart.superfinanciera.gov.co/api/storage/a.pdf", "id": "a", "type": "application/pdf"},
                {"file": "https://qasmart.superfinanciera.gov.co/api/storage/b.pdf", "id": "b", "type": "application/pdf"},
            ]
        )

        nombres = {r["nombre_archivo"] for r in resultado}
        self.assertEqual(nombres, {"a.pdf", "b.pdf"})


class TestDescargarAdjuntoStreaming(unittest.IsolatedAsyncioTestCase):

    async def test_content_length_excede_el_limite_lanza_file_size_exceeded(self):
        from app.core.exceptions import SfcIntegrationException
        import io

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Length": str(40 * 1024 * 1024)}, content=b"x")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            tmp_file = io.BytesIO()
            with self.assertRaises(SfcIntegrationException) as ctx:
                await S3StorageService._descargar_adjunto_streaming(client, "https://sfc.test/x.pdf", tmp_file, "x.pdf")
            self.assertEqual(ctx.exception.error_type, "FILE_SIZE_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
