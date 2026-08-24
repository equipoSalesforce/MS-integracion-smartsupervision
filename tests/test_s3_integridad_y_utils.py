# tests/test_s3_integridad_y_utils.py
"""
Cobertura de utilidades puras de S3StorageService: _limpiar_key,
normalizar_tipo_archivo y validar_integridad_archivo (verificación de Magic
Bytes contra la extensión declarada). Antes con varias ramas sin cubrir.
"""
import io
import unittest

from app.services.s3_service import S3StorageService
from app.core.exceptions import SfcIntegrationException


class TestLimpiarKey(unittest.TestCase):

    def test_vacio_retorna_vacio(self):
        self.assertEqual(S3StorageService._limpiar_key(""), "")
        self.assertEqual(S3StorageService._limpiar_key(None), "")

    def test_remueve_barras_iniciales(self):
        self.assertEqual(S3StorageService._limpiar_key("/caso/SC-1/doc.pdf"), "caso/SC-1/doc.pdf")


class TestNormalizarTipoArchivo(unittest.TestCase):

    def test_mime_type_conocido_en_el_mapa(self):
        self.assertEqual(S3StorageService.normalizar_tipo_archivo("application/pdf"), ("pdf", "application/pdf"))

    def test_mime_type_con_slash_no_mapeado_deriva_extension(self):
        ext, content_type = S3StorageService.normalizar_tipo_archivo("application/vnd.custom-report")
        self.assertEqual(ext, "custom-report")
        self.assertEqual(content_type, "application/vnd.custom-report")

    def test_sin_mime_type_deriva_extension_de_la_url(self):
        ext, content_type = S3StorageService.normalizar_tipo_archivo("", "https://sfc.test/archivo.docx")
        self.assertEqual(ext, "docx")
        self.assertEqual(content_type, "application/docx")

    def test_sin_mime_ni_url_usable_cae_a_pdf_por_defecto(self):
        self.assertEqual(S3StorageService.normalizar_tipo_archivo(""), ("pdf", "application/pdf"))


class TestValidarIntegridadArchivo(unittest.TestCase):

    def test_bytes_vacios_lanza_file_empty_error(self):
        with self.assertRaises(SfcIntegrationException) as ctx:
            S3StorageService.validar_integridad_archivo(file_data=b"", file_name="doc.pdf")
        self.assertEqual(ctx.exception.error_type, "FILE_EMPTY_ERROR")

    def test_bytes_con_magic_bytes_correcto_no_lanza(self):
        S3StorageService.validar_integridad_archivo(file_data=b"%PDF-1.4 contenido", file_name="doc.pdf")

    def test_bytes_con_magic_bytes_incorrecto_lanza_corrupted(self):
        with self.assertRaises(SfcIntegrationException) as ctx:
            S3StorageService.validar_integridad_archivo(file_data=b"no es un pdf real", file_name="doc.pdf")
        self.assertEqual(ctx.exception.error_type, "CORRUPTED_OR_INVALID_FILE")

    def test_file_obj_vacio_lanza_file_empty_error(self):
        with self.assertRaises(SfcIntegrationException) as ctx:
            S3StorageService.validar_integridad_archivo(file_data=io.BytesIO(b""), file_name="doc.zip")
        self.assertEqual(ctx.exception.error_type, "FILE_EMPTY_ERROR")

    def test_file_obj_con_contenido_valido_no_lanza_y_deja_cursor_al_inicio(self):
        file_obj = io.BytesIO(b"PK\x03\x04 contenido de zip")
        S3StorageService.validar_integridad_archivo(file_data=file_obj, file_name="doc.zip")
        self.assertEqual(file_obj.tell(), 0)

    def test_extension_no_reconocida_no_valida_magic_bytes(self):
        # ".txt" no está en magic_headers -- cualquier contenido no vacío pasa.
        S3StorageService.validar_integridad_archivo(file_data=b"contenido cualquiera", file_name="doc.txt")

    def test_sin_extension_en_el_nombre_no_valida_magic_bytes(self):
        S3StorageService.validar_integridad_archivo(file_data=b"contenido cualquiera", file_name="doc_sin_extension")


if __name__ == "__main__":
    unittest.main()
