# tests/test_clasificacion_operacion.py
"""
Regresión del hallazgo N9 (revisión externa v5, 2026-08-25): la misma expresión
booleana de "es cierre" vivía triplicada, de forma independiente, en crm_payloads.py,
despacho_queja_orchestrator.py e idempotency_service.py. Se consolida en
es_estado_cierre -- estos tests cubren la función compartida en sí; los tests de cada
consumidor (test_crm_payloads_validation.py, test_despacho_orquestador.py,
test_idempotency_*.py) siguen cubriendo la integración de cada uno.
"""
import unittest

from app.core.clasificacion_operacion import es_estado_cierre


class TestEsEstadoCierre(unittest.TestCase):

    def test_todos_ausentes_no_es_cierre(self):
        self.assertFalse(es_estado_cierre(None, None, None, None))

    def test_status_closed_es_cierre(self):
        self.assertTrue(es_estado_cierre("Closed", None, None, None))

    def test_status_cerrado_es_cierre(self):
        self.assertTrue(es_estado_cierre("Cerrado", None, None, None))

    def test_status_ignora_mayusculas_y_espacios(self):
        self.assertTrue(es_estado_cierre("  CLOSED  ", None, None, None))
        self.assertTrue(es_estado_cierre("  cerrado  ", None, None, None))

    def test_status_new_no_es_cierre(self):
        self.assertFalse(es_estado_cierre("New", None, None, None))

    def test_status_in_progress_no_es_cierre(self):
        self.assertFalse(es_estado_cierre("In Progress", None, None, None))

    def test_solo_closed_date_es_cierre(self):
        self.assertTrue(es_estado_cierre(None, "2026-01-01", None, None))

    def test_solo_favorabilidad_es_cierre(self):
        self.assertTrue(es_estado_cierre(None, None, "No favorable", None))

    def test_solo_aceptacion_es_cierre(self):
        """
        Confirmado con el equipo: Aceptacion__c sólo se popula junto con
        Favorabilidad__c en un cierre real (QuejaUnificadaCrmInput._validar_reglas_
        cierre exige ambos) -- esta condición no dispara sola en producción, pero la
        función la sigue cubriendo por si esa regla de negocio cambia.
        """
        self.assertTrue(es_estado_cierre(None, None, None, "Aceptada por el cliente"))

    def test_status_vacio_no_lanza(self):
        self.assertFalse(es_estado_cierre("", None, None, None))


if __name__ == "__main__":
    unittest.main()
