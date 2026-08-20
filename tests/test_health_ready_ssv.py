# tests/test_health_ready_ssv.py
import unittest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from app.main import app


class TestHealthReadySSV(unittest.TestCase):
    """
    Cubre GET /api/v1/quejas/_health/ready -- ruta propia de SSV (no /health/ready)
    para el smoke post-deploy sobre el ALB compartido con CRM Global66: un 200
    genérico no confirma que la respuesta venga del target group de SSV, por eso
    el contrato incluye "servicio".
    """

    def setUp(self):
        self.client = TestClient(app)

    @patch("app.api.routes_quejas.ping_redis", new_callable=AsyncMock, return_value=True)
    def test_redis_disponible_responde_200_con_contrato_ssv(self, mock_ping):
        resp = self.client.get("/api/v1/quejas/_health/ready")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"servicio": "SSV", "redis": True})

    @patch("app.api.routes_quejas.ping_redis", new_callable=AsyncMock, return_value=False)
    def test_redis_no_disponible_responde_503(self, mock_ping):
        resp = self.client.get("/api/v1/quejas/_health/ready")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"servicio": "SSV", "redis": False})

    def test_endpoint_no_requiere_api_key(self):
        """A diferencia del resto de rutas bajo /sync/*, esta no exige X-API-Key."""
        with patch("app.api.routes_quejas.ping_redis", new_callable=AsyncMock, return_value=True):
            resp = self.client.get("/api/v1/quejas/_health/ready")
        self.assertNotIn(resp.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
