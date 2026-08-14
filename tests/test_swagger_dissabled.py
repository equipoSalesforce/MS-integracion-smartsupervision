# tests/test_swagger_disabled.py
import unittest
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

        # 🟢 No reutilizar `app.main.app`: si otro módulo de test ya importó `app.main`
        # antes (import cacheado por sys.modules), su docs_url/openapi_url quedaron fijados
        # en ese primer import y parchear `settings` después no los cambia. En su lugar,
        # se reconstruye una instancia de FastAPI aislada con el `Settings` de producción.
        from app.main import _construir_app_fastapi

        app_prod = _construir_app_fastapi(cfg_prod)
        client = TestClient(app_prod)

        res_docs = client.get("/docs")
        res_redoc = client.get("/redoc")
        res_openapi = client.get("/api/v1/openapi.json")

        self.assertEqual(res_docs.status_code, 404)
        self.assertEqual(res_redoc.status_code, 404)
        self.assertEqual(res_openapi.status_code, 404)


if __name__ == "__main__":
    unittest.main()