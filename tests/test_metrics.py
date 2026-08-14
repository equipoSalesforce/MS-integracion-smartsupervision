# tests/test_metrics.py
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.core.metrics import emit_emf_metric


class TestEmitEmfMetric(unittest.TestCase):

    def test_emite_linea_json_valida_con_bloque_aws(self):
        """La línea emitida debe ser JSON válido con el bloque _aws en el nivel superior."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_emf_metric(
                namespace="SSV/Queue",
                metrics={"queue_depth": (12, "Count"), "oldest_pending_age_seconds": (730.5, "Seconds")}
            )

        linea = buf.getvalue().strip()
        obj = json.loads(linea)

        self.assertIn("_aws", obj)
        self.assertIn("Timestamp", obj["_aws"])
        directiva = obj["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(directiva["Namespace"], "SSV/Queue")
        nombres_metricas = {m["Name"] for m in directiva["Metrics"]}
        self.assertEqual(nombres_metricas, {"queue_depth", "oldest_pending_age_seconds"})

        # Los valores reales deben estar disponibles al nivel superior del objeto,
        # no anidados dentro de un campo "message" (a diferencia del logger JSON estándar).
        self.assertEqual(obj["queue_depth"], 12)
        self.assertEqual(obj["oldest_pending_age_seconds"], 730.5)

    def test_dimensiones_por_defecto_usa_environment(self):
        buf = io.StringIO()
        with patch("app.core.metrics.settings.ENVIRONMENT", "staging"), redirect_stdout(buf):
            emit_emf_metric(namespace="SSV/Queue", metrics={"queue_depth": (1, "Count")})

        obj = json.loads(buf.getvalue().strip())
        self.assertEqual(obj["Environment"], "staging")
        self.assertEqual(obj["_aws"]["CloudWatchMetrics"][0]["Dimensions"], [["Environment"]])

    def test_dimensiones_personalizadas(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_emf_metric(
                namespace="SSV/Queue",
                metrics={"queue_depth": (1, "Count")},
                dimensions={"Environment": "prod", "Job": "retry"}
            )

        obj = json.loads(buf.getvalue().strip())
        self.assertEqual(obj["Environment"], "prod")
        self.assertEqual(obj["Job"], "retry")
        self.assertEqual(obj["_aws"]["CloudWatchMetrics"][0]["Dimensions"], [["Environment", "Job"]])

    def test_metrics_vacio_no_escribe_nada(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_emf_metric(namespace="SSV/Queue", metrics={})

        self.assertEqual(buf.getvalue(), "")

    def test_fallo_de_escritura_no_propaga_excepcion(self):
        """Un fallo al escribir a stdout no debe tumbar al caller (best-effort)."""
        with patch("sys.stdout.write", side_effect=OSError("broken pipe")):
            try:
                emit_emf_metric(namespace="SSV/Queue", metrics={"queue_depth": (1, "Count")})
            except Exception as e:
                self.fail(f"emit_emf_metric no debería propagar excepciones, lanzó: {e}")


if __name__ == "__main__":
    unittest.main()
