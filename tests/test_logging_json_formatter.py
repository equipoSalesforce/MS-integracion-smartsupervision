# tests/test_logging_json_formatter.py
"""
Cobertura de JSONFormatter (app/core/logging_config.py) y el contrato
`extra={"extra_data": {...}}` que dependen todos los logs AUDIT_HTTP_* del
repo (sfc_client.py, crm_webhook_service.py, routes_quejas.py).

🔴 FIX (hallazgo de revisión externa, 2026-08-25): JSONFormatter.format sólo
lee `record.extra_data`. logging.Logger.info(msg, extra={...}) inyecta cada
clave del dict directamente como atributo del LogRecord (record.direction,
record.body, etc.) -- NO bajo `record.extra_data`, a menos que el propio dict
pasado a `extra=` tenga una única clave literal 'extra_data'. El log de
auditoría de entrada del CRM (despachar_queja_crm) pasaba un dict plano
(`extra={"direction": ..., "body": ...}`), así que en producción el campo
"extra" nunca aparecía en el JSON -- el único rastro del lado de entrada de
toda la cadena de auditoría regulatoria se perdía en silencio.
"""
import json
import logging
import unittest

from app.core.logging_config import JSONFormatter


class TestJSONFormatterExtraData(unittest.TestCase):

    def setUp(self):
        self.formatter = JSONFormatter()

    @staticmethod
    def _hacer_record(extra: dict) -> logging.LogRecord:
        logger = logging.getLogger("test.jsonformatter")
        return logger.makeRecord(
            name="test.jsonformatter", level=logging.INFO, fn="test", lno=1,
            msg="AUDIT_TEST", args=(), exc_info=None, extra=extra
        )

    def test_extra_data_anidado_correctamente_se_incluye_en_extra(self):
        """El patrón correcto (ya usado por sfc_client.py): envolver el payload de
        auditoría bajo la clave literal 'extra_data'."""
        record = self._hacer_record({
            "extra_data": {"direction": "INCOMING_REQUEST", "body": {"a": 1}}
        })
        log_obj = json.loads(self.formatter.format(record))
        self.assertEqual(log_obj["extra"], {"direction": "INCOMING_REQUEST", "body": {"a": 1}})

    def test_extra_plano_sin_envolver_no_produce_la_clave_extra(self):
        """
        Regresión del bug real: extra={...} PLANO (como tenía
        despachar_queja_crm antes del fix) no produce 'extra' en el JSON final --
        las claves quedan como atributos sueltos del record, invisibles para
        JSONFormatter.
        """
        record = self._hacer_record({"direction": "INCOMING_REQUEST", "body": {"a": 1}})
        log_obj = json.loads(self.formatter.format(record))
        self.assertNotIn("extra", log_obj)
        # Las claves sí quedaron como atributos sueltos del record -- confirma el
        # mecanismo exacto del bug, no sólo su síntoma.
        self.assertEqual(record.direction, "INCOMING_REQUEST")

    def test_sin_extra_no_lanza_y_no_incluye_la_clave(self):
        record = self._hacer_record({})
        log_obj = json.loads(self.formatter.format(record))
        self.assertNotIn("extra", log_obj)


if __name__ == "__main__":
    unittest.main()
