# tests/test_config_edge_cases.py
"""
Cobertura de ramas de app/core/config.py no cubiertas por
test_config_security_validation.py: los parsers standalone parse_cors/
parse_email_list (formato JSON-array y tipos inválidos), el rechazo de un
ENVIRONMENT no reconocido, las ramas restantes de _validar_webhook_crm
(URL ausente/example.com, esquema no-http(s), API key ausente), y
ENABLE_DOCS activo en producción.
"""
import unittest

from pydantic import ValidationError

from app.core.config import Settings, parse_cors, parse_email_list


class TestParseCors(unittest.TestCase):

    def test_formato_json_array(self):
        self.assertEqual(parse_cors('["https://a.com", "https://b.com"]'), ["https://a.com", "https://b.com"])

    def test_json_invalido_cae_a_parseo_por_comas(self):
        # Empieza y termina con corchetes pero no es JSON válido -- debe caer al
        # parseo por comas en vez de lanzar.
        self.assertEqual(parse_cors("[no es json]"), ["[no es json]"])

    def test_tipo_invalido_lanza_value_error(self):
        with self.assertRaises(ValueError):
            parse_cors(12345)


class TestParseEmailList(unittest.TestCase):

    def test_formato_json_array(self):
        self.assertEqual(parse_email_list('["a@g66.com", "b@g66.com"]'), ["a@g66.com", "b@g66.com"])

    def test_json_invalido_cae_a_parseo_por_comas(self):
        self.assertEqual(parse_email_list("[no es json]"), ["[no es json]"])

    def test_tipo_invalido_lanza_value_error(self):
        with self.assertRaises(ValueError):
            parse_email_list(12345)


def _kwargs_produccion_validos(**overrides) -> dict:
    base = dict(
        _env_file=None,
        ENVIRONMENT="production",
        CRM_CORS_ORIGINS="https://crm.global66.com",
        AWS_S3_BUCKET="prod-global66-smartsupervision-attachments",
        AWS_REGION="us-east-1",
        SFC_URL_BASE="https://smart.superfinanciera.gov.co",
        SFC_USERNAME="prod_sfc_user",
        SFC_PASSWORD="PasswordSeguroProductivo2026#$",
        SFC_SECRET_KEY="clave_hmac_secreta_real_otorgada_por_sfc_2026",
        CRM_API_KEY="g66_sk_prod_real_key_xyz_987654321",
        ADMIN_API_KEY="g66_sk_prod_admin_real_key_abc_123456789",
        CRM_WEBHOOK_URL="https://crm.global66.com/api/v1/webhooks/sfc",
        CRM_WEBHOOK_API_KEY="wh_prod_key_999888777",
        SMTP_USER="alertas_prod@global66.com",
        SMTP_PASSWORD="SmtpPasswordSegura2026!",
        ALERT_NOTIFY_EMAILS="ops@global66.com",
        REDIS_HOST="prod-smartsupervision-redis.abc123.use1.cache.amazonaws.com",
        REDIS_PASSWORD="RedisPasswordSeguroProductivo2026#$",
        REDIS_SSL=True,
    )
    base.update(overrides)
    return base


class TestEnvironmentDesconocido(unittest.TestCase):

    def test_environment_no_reconocido_lanza_fail_fast(self):
        with self.assertRaises(ValidationError) as ctx:
            Settings(_env_file=None, ENVIRONMENT="produccion-mal-escrito")
        self.assertIn("FAIL-FAST", str(ctx.exception))
        self.assertIn("ENVIRONMENT", str(ctx.exception))


class TestValidarWebhookCrm(unittest.TestCase):

    def test_webhook_url_ausente_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Settings(**_kwargs_produccion_validos(CRM_WEBHOOK_URL=""))
        self.assertIn("CRM_WEBHOOK_URL", str(ctx.exception))

    def test_webhook_url_example_com_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Settings(**_kwargs_produccion_validos(CRM_WEBHOOK_URL="https://webhook.example.com/sfc"))
        self.assertIn("CRM_WEBHOOK_URL", str(ctx.exception))

    def test_webhook_url_con_esquema_no_http_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Settings(**_kwargs_produccion_validos(CRM_WEBHOOK_URL="ftp://crm.global66.com/webhook"))
        self.assertIn("no es una URL http(s)", str(ctx.exception))

    def test_webhook_api_key_ausente_es_rechazada(self):
        with self.assertRaises(ValidationError) as ctx:
            Settings(**_kwargs_produccion_validos(CRM_WEBHOOK_API_KEY=""))
        self.assertIn("CRM_WEBHOOK_API_KEY", str(ctx.exception))


class TestValidarDocsExpuestos(unittest.TestCase):

    def test_enable_docs_activo_en_produccion_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Settings(**_kwargs_produccion_validos(ENABLE_DOCS=True))
        self.assertIn("ENABLE_DOCS", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
