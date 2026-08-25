# tests/test_momento_2_es_error_ya_existe.py
"""
Cobertura de _es_error_queja_ya_existe_m2 (app/services/momento_2_sync.py) --
sin cobertura directa previa.

🔴 FIX (hallazgo de revisión externa, 2026-08-25): la función distingue "la
SFC rechazó porque el MISMO código de queja ya existe" (tolerable, éxito
idempotente vía _crear_queja_o_tolerar_duplicado) de "la SFC rechazó por la
regla anti-duplicados funcional de motivo/producto/canal" (una queja
DISTINTA, debe propagarse como fallo real). El chequeo de error_type ==
"ALREADY_EXISTS" (señal estructurada y confiable) vivía DESPUÉS del match de
frases de colisión funcional, que incluye la subcadena "already_exist" --
el propio nombre del error_type. Un ALREADY_EXISTS real cuyo mensaje
contuviera esa subcadena se clasificaba mal como colisión funcional.
"""
import unittest

from app.services.momento_2_sync import _es_error_queja_ya_existe_m2


class TestEsErrorQuejaYaExisteM2(unittest.TestCase):

    def test_error_type_already_exists_con_la_subcadena_en_el_mensaje_se_tolera(self):
        """El escenario exacto del hallazgo: error_type estructurado ALREADY_EXISTS,
        pero el mensaje libre también contiene la subcadena 'already_exist'."""
        self.assertTrue(
            _es_error_queja_ya_existe_m2(
                exc_raw_msg="ALREADY_EXISTS: el codigo_queja ya fue registrado previamente",
                error_type="ALREADY_EXISTS"
            )
        )

    def test_error_type_already_exists_sin_texto_relacionado_se_tolera(self):
        self.assertTrue(_es_error_queja_ya_existe_m2(exc_raw_msg="detalle genérico", error_type="ALREADY_EXISTS"))

    def test_colision_funcional_por_mismo_motivo_no_se_tolera(self):
        self.assertFalse(
            _es_error_queja_ya_existe_m2(
                exc_raw_msg="Ya existe una queja abierta con el mismo motivo y producto para este cliente",
                error_type=None
            )
        )

    def test_colision_funcional_por_texto_already_exist_sin_error_type_no_se_tolera(self):
        """Sin la señal estructurada, el match de frases (incluyendo 'already_exist'
        como texto libre) sigue aplicando -- comportamiento previo preservado."""
        self.assertFalse(
            _es_error_queja_ya_existe_m2(
                exc_raw_msg="funcional_already_exist_rule triggered: verifique el motivo",
                error_type=None
            )
        )

    def test_mensaje_libre_ya_existe_sin_error_type_estructurado_se_tolera(self):
        self.assertTrue(_es_error_queja_ya_existe_m2(exc_raw_msg="La queja ya existe en la base de datos", error_type=None))

    def test_mensaje_libre_registrado_en_la_sfc_se_tolera(self):
        self.assertTrue(_es_error_queja_ya_existe_m2(exc_raw_msg="El caso ya está registrado en la SFC", error_type="OTRO_TIPO"))

    def test_error_no_relacionado_no_se_tolera(self):
        self.assertFalse(_es_error_queja_ya_existe_m2(exc_raw_msg="Producto no encontrado en catálogo", error_type="CATALOG_ERROR"))

    def test_mensaje_vacio_no_se_tolera(self):
        self.assertFalse(_es_error_queja_ya_existe_m2(exc_raw_msg="", error_type=None))
        self.assertFalse(_es_error_queja_ya_existe_m2(exc_raw_msg=None, error_type=None))


if __name__ == "__main__":
    unittest.main()
