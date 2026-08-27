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
            SFC_SECRET_KEY="hmac_key_real_no_generico_2026",
            CRM_API_KEY="crm_key_real_no_generico_2026",
            ADMIN_API_KEY="admin_key_real_no_generico_2026",
            CRM_WEBHOOK_URL="https://crm.global66.com/webhook",
            CRM_WEBHOOK_API_KEY="wh_key",
            SMTP_USER="user@g66.com",
            SMTP_PASSWORD="pwd",
            ALERT_NOTIFY_EMAILS="alert@g66.com",
            REDIS_HOST="prod-smartsupervision-redis.abc123.use1.cache.amazonaws.com",
            REDIS_PASSWORD="redis_pwd_segura",
            REDIS_SSL=True
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

    def test_docs_deshabilitados_en_ambiente_development_por_defecto(self):
        """
        🔴 FIX (hallazgo propio, 2026-08-27): _construir_app_fastapi trataba
        ENVIRONMENT='development' como equivalente a 'local' -- exponía Swagger/
        ReDoc/OpenAPI sin necesitar ENABLE_DOCS=True. 'development' debe requerir
        el mismo opt-in explícito que cualquier otro ambiente no-local; sólo
        'local' (el único valor que nunca se despliega de verdad) los habilita
        por defecto.
        """
        cfg_dev = Settings(
            _env_file=None,
            ENVIRONMENT="development",
            ENABLE_DOCS=False,
            CRM_CORS_ORIGINS="https://crm.global66.com",
            AWS_S3_BUCKET="dev-bucket",
            AWS_REGION="us-east-1",
            SFC_URL_BASE="https://qasmart.superfinanciera.gov.co",
            SFC_USERNAME="usr",
            SFC_PASSWORD="pwd",
            SFC_SECRET_KEY="hmac_key_real_no_generico_2026",
            CRM_API_KEY="crm_key_real_no_generico_2026",
            ADMIN_API_KEY="admin_key_real_no_generico_2026",
            CRM_WEBHOOK_URL="https://crm.global66.com/webhook",
            CRM_WEBHOOK_API_KEY="wh_key",
            SMTP_USER="user@g66.com",
            SMTP_PASSWORD="pwd",
            ALERT_NOTIFY_EMAILS="alert@g66.com",
        )

        from app.main import _construir_app_fastapi

        app_dev = _construir_app_fastapi(cfg_dev)
        client = TestClient(app_dev)

        self.assertEqual(client.get("/docs").status_code, 404)
        self.assertEqual(client.get("/redoc").status_code, 404)
        self.assertEqual(client.get("/api/v1/openapi.json").status_code, 404)

    def test_docs_habilitados_en_local_sin_flag_explicito(self):
        """Contraprueba: 'local' -- el único ambiente que nunca se despliega de
        verdad -- sigue habilitando docs por defecto, sin requerir ENABLE_DOCS."""
        cfg_local = Settings(_env_file=None, ENVIRONMENT="local")

        from app.main import _construir_app_fastapi

        app_local = _construir_app_fastapi(cfg_local)
        client = TestClient(app_local)

        self.assertEqual(client.get("/docs").status_code, 200)


if __name__ == "__main__":
    unittest.main()