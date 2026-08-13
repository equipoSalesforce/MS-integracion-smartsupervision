# tests/test_queue_correlation_id.py
import unittest
from app.services.queue_service import ColaItemRedis


class TestQueueCorrelationId(unittest.TestCase):

    def test_cola_item_redis_preserva_y_serializa_correlation_id(self):
        """
        Verifica que ColaItemRedis extraiga el correlation_id original del diccionario
        y lo preserve en to_dict() y to_summary_dict().
        """
        data = {
            "id": 42,
            "smart_code": "1286SC-TEST-99",
            "tipo_operacion": "AUTO",
            "payload_json": {"Case_id": "SC-TEST-99"},
            "estado": "PENDING",
            "intentos": 1,
            "max_intentos": 10,
            "created_at": "2026-08-13T10:00:00",
            "updated_at": "2026-08-13T10:00:00",
            "correlation_id": "CID-ORIGINAL-TEST-999"
        }

        item = ColaItemRedis(data)

        # 1. Atributo de la instancia
        self.assertEqual(item.correlation_id, "CID-ORIGINAL-TEST-999")

        # 2. Serialización completa to_dict()
        dict_data = item.to_dict()
        self.assertIn("correlation_id", dict_data)
        self.assertEqual(dict_data["correlation_id"], "CID-ORIGINAL-TEST-999")

        # 3. Serialización resumida to_summary_dict()
        summary_data = item.to_summary_dict()
        self.assertIn("correlation_id", summary_data)
        self.assertEqual(summary_data["correlation_id"], "CID-ORIGINAL-TEST-999")


if __name__ == "__main__":
    unittest.main()