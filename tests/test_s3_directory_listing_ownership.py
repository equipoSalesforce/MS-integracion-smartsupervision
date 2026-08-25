# tests/test_s3_directory_listing_ownership.py
"""
Regresión de un hallazgo de revisión externa (2026-08-25): dos debilidades
combinadas en el listado dinámico de archivos por directorio_s3 permitían
exfiltrar el bucket completo bajo un solo despacho:

1. _limpiar_key("/") devuelve "" (sólo remueve barras iniciales), y
   listar_archivos_en_directorio sólo añadía la barra final `if prefix_clean`
   (falsy para ""), así que un directorio_s3="/" terminaba en
   list_objects_v2(Prefix="") -- el BUCKET COMPLETO, no un caso específico.

2. _validar_ownership_key comparaba PERTENENCIA DE SEGMENTO (case_id_esperado
   en cualquier parte de la ruta), no la carpeta contenedora real del
   archivo. Con la convención real "caso/{case_id}/archivo", un Case_id="caso"
   (sin restricción de formato más allá de letras/números/guiones en
   crm_payloads.py) pasaba la validación de ownership contra CUALQUIER
   archivo de CUALQUIER caso, porque "caso" es un segmento presente en todas
   las keys.

Combinadas: directorio_s3="/" + Case_id="caso" listaba y transmitía a la SFC
los adjuntos de todos los clientes bajo un solo codigo_queja.

El fix: (a) listar_archivos_en_directorio rechaza un prefijo vacío/raíz
incondicionalmente, y (b) valida que el prefijo pertenezca al caso ANTES de
listar; (c) _validar_ownership_key ahora exige que case_id_esperado sea
EXACTAMENTE la carpeta contenedora directa del archivo (el penúltimo
segmento), no sólo estar presente en algún lugar de la ruta -- sin asumir un
prefijo fijo, porque el repo usa varias convenciones reales distintas
("caso/{id}/archivo.pdf", "{id}/archivo.pdf", "quejas/{id}/archivo.pdf").
"""
import unittest
from unittest.mock import MagicMock

from app.core.exceptions import SfcIntegrationException
from app.services.s3_service import S3StorageService


class TestValidarOwnershipKeyPrefijoNoSegmento(unittest.TestCase):

    def setUp(self):
        self.service = S3StorageService(s3_client=None)

    def test_case_id_igual_al_segmento_fijo_caso_ya_no_pasa_para_otro_cliente(self):
        """El bypass reportado: Case_id='caso' coincide con el segmento fijo de la
        convención de rutas -- antes eso bastaba para 'poseer' cualquier archivo."""
        with self.assertRaises(SfcIntegrationException) as ctx:
            self.service._validar_ownership_key("caso/OTRO-CLIENTE-123/extracto.pdf", "caso")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.error_type, "S3_KEY_OWNERSHIP_MISMATCH")

    def test_case_id_real_en_la_posicion_correcta_si_pasa(self):
        self.service._validar_ownership_key("caso/MI-CASO-123/extracto.pdf", "MI-CASO-123")  # No debe lanzar.

    def test_case_id_de_otro_cliente_es_rechazado(self):
        with self.assertRaises(SfcIntegrationException):
            self.service._validar_ownership_key("caso/OTRO-CLIENTE-123/extracto.pdf", "MI-CASO-123")

    def test_convencion_sin_carpeta_intermedia_tambien_es_valida(self):
        """No hay un único prefijo fijo en el repo -- 'quejas/{id}/archivo.pdf' y
        '{id}/archivo.pdf' (sin ninguna carpeta antes) también son válidos,
        siempre que case_id sea la carpeta contenedora DIRECTA del archivo."""
        self.service._validar_ownership_key("quejas/MI-CASO-123/extracto.pdf", "MI-CASO-123")  # No debe lanzar.
        self.service._validar_ownership_key("MI-CASO-123/extracto.pdf", "MI-CASO-123")  # No debe lanzar.

    def test_case_id_presente_pero_no_como_carpeta_contenedora_directa_es_rechazado(self):
        """El case_id aparece en la ruta (primer segmento), pero la carpeta que
        realmente contiene el archivo es otra -- no basta con 'aparecer en algún
        lugar', debe ser específicamente el padre directo del archivo."""
        with self.assertRaises(SfcIntegrationException):
            self.service._validar_ownership_key("MI-CASO-123/otra-carpeta/extracto.pdf", "MI-CASO-123")

    def test_sin_case_id_esperado_no_valida_nada(self):
        self.service._validar_ownership_key("caso/CUALQUIERA/x.pdf", None)  # No debe lanzar (comportamiento previo).


class TestListarArchivosEnDirectorioRechazaPrefijoRaiz(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_s3 = MagicMock()
        self.service = S3StorageService(s3_client=self.mock_s3)

    async def test_prefijo_barra_no_lista_el_bucket_completo(self):
        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.service.listar_archivos_en_directorio(prefix="/", case_id_esperado="MI-CASO-123")
        self.assertEqual(ctx.exception.status_code, 400)
        self.mock_s3.get_paginator.assert_not_called()

    async def test_prefijo_vacio_no_lista_el_bucket_completo(self):
        with self.assertRaises(SfcIntegrationException):
            await self.service.listar_archivos_en_directorio(prefix="", case_id_esperado="MI-CASO-123")
        self.mock_s3.get_paginator.assert_not_called()

    async def test_prefijo_raiz_se_rechaza_incluso_sin_case_id_esperado(self):
        """Defensa en profundidad: un caller futuro que olvide pasar case_id_esperado
        no debe poder reabrir el listado del bucket completo."""
        with self.assertRaises(SfcIntegrationException):
            await self.service.listar_archivos_en_directorio(prefix="/")
        self.mock_s3.get_paginator.assert_not_called()

    async def test_prefijo_de_otro_caso_es_rechazado(self):
        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.service.listar_archivos_en_directorio(
                prefix="caso/OTRO-CLIENTE-123/", case_id_esperado="MI-CASO-123"
            )
        self.assertEqual(ctx.exception.error_type, "S3_KEY_OWNERSHIP_MISMATCH")
        self.mock_s3.get_paginator.assert_not_called()

    async def test_prefijo_del_propio_caso_si_lista_correctamente(self):
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {"Contents": [{"Key": "caso/MI-CASO-123/soporte.pdf"}]}
        ]
        self.mock_s3.get_paginator.return_value = mock_paginator

        archivos = await self.service.listar_archivos_en_directorio(
            prefix="caso/MI-CASO-123/", case_id_esperado="MI-CASO-123"
        )

        self.assertEqual(len(archivos), 1)
        self.assertEqual(archivos[0]["s3_key"], "caso/MI-CASO-123/soporte.pdf")


if __name__ == "__main__":
    unittest.main()
