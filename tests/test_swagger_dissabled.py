# tests/test_swagger_disabled.py
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.core.config import Settings


class TestSwaggerDisabledInNonLocalEnvironments(unittest.TestCase):

    def test_docs_deshabilitados_en_produccion(self):
        """
        Verifica que en ambiente 'production' las rutas /docs, /redoc
        y /api/v1/openapi.json retornen 404 Not Found.
        """
        cfg_prod = Settings(
            _env_file=None,
            ENVIRONMENT="production",
            ENABLE_DOCS=False,
            CRM_CORS_ORIGINS="https://crm.global66.com",
            AWS_S3_BUCKET="prod-bucket",
            AWS_REGION="us-east-1",
            SFC_URL_BASE="https://smart.superfinanciera.gov.co",
            SFC_USERNAME="usr",
            SFC_PASSWORD="pwd",
            SFC_SECRET_KEY="hmac_key",
            CRM_API_KEY="crm_key",
            ADMIN_API_KEY="admin_key",
            CRM_WEBHOOK_URL="https://crm.global66.com/webhook",
            SMTP_USER="user@g66.com",
            SMTP_PASSWORD="pwd",
            ALERT_NOTIFY_EMAILS="alert@g66.com"
        )

        with patch("app.main.settings", cfg_prod):
            from app.main import app
            client = TestClient(app)

            res_docs = client.get("/docs")
            res_redoc = client.get("/redoc")
            res_openapi = client.get("/api/v1/openapi.json")

            self.assertEqual(res_docs.status_code, 404)
            self.assertEqual(res_redoc.status_code, 404)
            self.assertEqual(res_openapi.status_code, 404)


if __name__ == "__main__":
    unittest.main()