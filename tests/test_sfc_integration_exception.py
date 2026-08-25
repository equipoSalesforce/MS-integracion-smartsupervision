# tests/test_sfc_integration_exception.py
"""
Cobertura de SfcIntegrationException.es_transitoria (app/core/exceptions.py).

🔴 FIX (hallazgo de revisión externa, 2026-08-25): antes esta clasificación
("¿es una caída transitoria de infraestructura de la SFC, o un rechazo de
negocio real?") vivía duplicada -- routes_quejas.py::es_error_contingencia la
calculaba sobre campos estructurados (status_code/error_type), mientras que
scheduler.py::_es_falla_infraestructura hacía match de subcadenas sobre el
texto libre del mensaje de error. Ahora es una única property reutilizada por
ambos call sites.
"""
import unittest

from app.core.exceptions import SfcIntegrationException


def _exc(status_code, error_type="ALGUN_ERROR", raw_message="detalle") -> SfcIntegrationException:
    return SfcIntegrationException(
        status_code=status_code, error_type=error_type, sfc_field=None,
        raw_message=raw_message, crm_action="Corrija el dato."
    )


class TestEsTransitoria(unittest.TestCase):

    def test_5xx_es_transitoria(self):
        self.assertTrue(_exc(500).es_transitoria)
        self.assertTrue(_exc(502).es_transitoria)
        self.assertTrue(_exc(504).es_transitoria)

    def test_429_es_transitoria(self):
        self.assertTrue(_exc(429, error_type="RATE_LIMIT_ERROR").es_transitoria)

    def test_503_es_transitoria(self):
        self.assertTrue(_exc(503).es_transitoria)

    def test_error_type_transitorio_con_status_code_de_negocio_es_transitoria(self):
        """El error_type ya clasificado alcanza aunque el status_code no sea 5xx/429/503."""
        self.assertTrue(_exc(400, error_type="TIMEOUT").es_transitoria)

    def test_4xx_de_negocio_no_es_transitoria(self):
        self.assertFalse(_exc(400, error_type="CRM_PAYLOAD_VALIDATION_ERROR").es_transitoria)
        self.assertFalse(_exc(403, error_type="S3_KEY_OWNERSHIP_MISMATCH").es_transitoria)
        self.assertFalse(_exc(404, error_type="NOT_FOUND_ERROR").es_transitoria)

    def test_mensaje_con_digitos_coincidentes_no_afecta_la_clasificacion(self):
        """El escenario del hallazgo: el texto libre del mensaje puede mencionar '503'
        como parte de un monto/código de caso -- sólo status_code/error_type deciden."""
        exc = _exc(400, error_type="CRM_PAYLOAD_VALIDATION_ERROR", raw_message="El monto (503829.50) supera el límite")
        self.assertFalse(exc.es_transitoria)


if __name__ == "__main__":
    unittest.main()
