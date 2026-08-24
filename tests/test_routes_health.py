# tests/test_routes_health.py
"""Cobertura de app/api/routes_health.py (/health/live, /health/ready) -- antes 54%."""
import unittest
from unittest.mock import patch, AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_health


def _crear_app_minima() -> FastAPI:
    app = FastAPI()
    app.include_router(routes_health.router)
    return app


class TestRoutesHealth(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(_crear_app_minima())

    def test_liveness_siempre_200(self):
        res = self.client.get("/health/live")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "alive"})

    def test_readiness_ok_cuando_redis_responde(self):
        with patch.object(routes_health, "ping_redis", new_callable=AsyncMock, return_value=True):
            res = self.client.get("/health/ready")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ready", "redis": True})

    def test_readiness_503_cuando_redis_no_responde(self):
        with patch.object(routes_health, "ping_redis", new_callable=AsyncMock, return_value=False):
            res = self.client.get("/health/ready")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json(), {"status": "unhealthy", "redis": False})


if __name__ == "__main__":
    unittest.main()
