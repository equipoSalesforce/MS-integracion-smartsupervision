import unittest
from unittest.mock import MagicMock
from app.services.s3_service import S3StorageService


class TestS3Pagination(unittest.IsolatedAsyncioTestCase):

    async def test_listar_archivos_mas_de_1000_objetos(self):
        """
        Verifica que listar_archivos_en_directorio recupere la totalidad de los archivos
        cuando la respuesta de S3 supera el límite de 1,000 objetos por página.
        """
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()

        # Simular 1,500 objetos en S3 divididos en 2 páginas (1,000 + 500)
        page1 = {"Contents": [{"Key": f"casos/001/doc_{i}.pdf"} for i in range(1000)]}
        page2 = {"Contents": [{"Key": f"casos/001/doc_{i}.pdf"} for i in range(1000, 1500)]}

        mock_paginator.paginate.return_value = [page1, page2]
        mock_s3.get_paginator.return_value = mock_paginator

        service = S3StorageService(s3_client=mock_s3)
        archivos = await service.listar_archivos_en_directorio(prefix="casos/001/")

        # Aserciones
        self.assertEqual(len(archivos), 1500, "Se esperaba recuperar los 1,500 archivos sin truncamiento.")
        self.assertEqual(archivos[0]["nombre_archivo"], "doc_0.pdf")
        self.assertEqual(archivos[1499]["nombre_archivo"], "doc_1499.pdf")
        mock_s3.get_paginator.assert_called_once_with("list_objects_v2")


if __name__ == "__main__":
    unittest.main()