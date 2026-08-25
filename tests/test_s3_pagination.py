import unittest
from unittest.mock import MagicMock
from app.services.s3_service import S3StorageService, MAX_ARCHIVOS_DIRECTORIO_S3
from app.core.exceptions import SfcIntegrationException


class TestS3Pagination(unittest.IsolatedAsyncioTestCase):

    async def test_listar_archivos_agrega_correctamente_a_traves_de_varias_paginas(self):
        """
        Verifica que listar_archivos_en_directorio recupere la totalidad de los archivos
        cuando la respuesta de S3 abarca varias páginas, siempre que el total no supere
        el cap (MAX_ARCHIVOS_DIRECTORIO_S3).
        """
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()

        # 30 objetos en la primera página, 15 en la segunda -- 45 en total, bajo el cap.
        page1 = {"Contents": [{"Key": f"casos/001/doc_{i}.pdf"} for i in range(30)]}
        page2 = {"Contents": [{"Key": f"casos/001/doc_{i}.pdf"} for i in range(30, 45)]}

        mock_paginator.paginate.return_value = [page1, page2]
        mock_s3.get_paginator.return_value = mock_paginator

        service = S3StorageService(s3_client=mock_s3)
        archivos = await service.listar_archivos_en_directorio(prefix="casos/001/")

        self.assertEqual(len(archivos), 45, "Se esperaba recuperar los 45 archivos sin truncamiento.")
        self.assertEqual(archivos[0]["nombre_archivo"], "doc_0.pdf")
        self.assertEqual(archivos[44]["nombre_archivo"], "doc_44.pdf")
        mock_s3.get_paginator.assert_called_once_with("list_objects_v2")

    async def test_listar_archivos_que_supera_el_cap_lanza_excepcion(self):
        """
        🟡 FIX (hallazgo A3, auditoría adversarial 2026-08-25): el listado dinámico vía
        directorio_s3 no tenía ningún límite propio -- un caso legítimo con una carpeta
        muy grande podía descargar/reenviar cientos de adjuntos en un único despacho.
        Ahora se rechaza en cuanto se supera el mismo tope que ya aplica a 'archivos_s3'
        (max_length=50), en vez de procesar la totalidad sin freno.
        """
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()

        page1 = {"Contents": [{"Key": f"casos/001/doc_{i}.pdf"} for i in range(1000)]}
        page2 = {"Contents": [{"Key": f"casos/001/doc_{i}.pdf"} for i in range(1000, 1500)]}

        mock_paginator.paginate.return_value = [page1, page2]
        mock_s3.get_paginator.return_value = mock_paginator

        service = S3StorageService(s3_client=mock_s3)

        with self.assertRaises(SfcIntegrationException) as ctx:
            await service.listar_archivos_en_directorio(prefix="casos/001/")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.error_type, "CRM_PAYLOAD_VALIDATION_ERROR")
        self.assertEqual(ctx.exception.sfc_field, "directorio_s3")

    async def test_listar_archivos_exactamente_en_el_cap_no_lanza(self):
        """Caso límite: exactamente MAX_ARCHIVOS_DIRECTORIO_S3 archivos debe pasar."""
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()

        page = {"Contents": [{"Key": f"casos/001/doc_{i}.pdf"} for i in range(MAX_ARCHIVOS_DIRECTORIO_S3)]}
        mock_paginator.paginate.return_value = [page]
        mock_s3.get_paginator.return_value = mock_paginator

        service = S3StorageService(s3_client=mock_s3)
        archivos = await service.listar_archivos_en_directorio(prefix="casos/001/")

        self.assertEqual(len(archivos), MAX_ARCHIVOS_DIRECTORIO_S3)


if __name__ == "__main__":
    unittest.main()