# tests/test_config_security_validation.py
import unittest
from pydantic import ValidationError
from app.core.config import Settings


class TestConfigSecurityValidation(unittest.TestCase):

    def test_startup_exitoso_en_ambiente_local_con_defaults(self):
        """
        Verifica que en ambiente 'local' el microservicio permita
        los valores por defecto de desarrollo.
        """
        cfg_local = Settings(_env_file=None, ENVIRONMENT="local")
        self.assertEqual(cfg_local.ENVIRONMENT, "local")

    def test_startup_fallido_en_produccion_con_defaults_inseguros(self):
        """
        Verifica el principio Fail-Fast: en ambiente 'production' debe lanzar
        ValidationError si no se reemplazaron las claves por defecto de prueba o si
        se utiliza el comodín '*' en CORS.
        """
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                ENVIRONMENT="production",
                CRM_CORS_ORIGINS="*",
                CRM_API_KEY="g66_sk_test_super_secreto_12345",
                ADMIN_API_KEY="g66_sk_test_admin_secreto_99999",
                SFC_SECRET_KEY="global66_sfc_secret_key_testing_2026",
                SFC_PASSWORD="123456789"
            )

        error_str = str(ctx.exception)
        self.assertIn("RIESGO CRÍTICO DE SEGURIDAD", error_str)
        self.assertIn("CRM_API_KEY", error_str)

    def test_startup_exitoso_en_produccion_con_secretos_reales(self):
        """
        Verifica que en producción el servicio arranque sin problemas cuando
        todas las variables secretas e infraestructura hayan sido inyectadas
        adecuadamente (dominios CORS explícitos y URLs reales).
        """
        cfg_prod = Settings(
            _env_file=None,
            ENVIRONMENT="production",
            CRM_CORS_ORIGINS="https://crm.global66.com,https://admin.global66.com",
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
            REDIS_SSL=True
        )
        self.assertEqual(cfg_prod.ENVIRONMENT, "production")
        self.assertEqual(cfg_prod.CRM_API_KEY, "g66_sk_prod_real_key_xyz_987654321")

    def test_sfc_url_base_http_rechazado_en_produccion(self):
        """
        P1-06 (auditoría adversarial v10): SFC_URL_BASE es el endpoint público de un
        ente regulador financiero (Superintendencia Financiera de Colombia) -- http://
        sin cifrar debe rechazarse siempre en ambientes desplegables, sin excepción.
        """
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                ENVIRONMENT="production",
                CRM_CORS_ORIGINS="https://crm.global66.com",
                AWS_S3_BUCKET="prod-global66-smartsupervision-attachments",
                AWS_REGION="us-east-1",
                SFC_URL_BASE="http://smart.superfinanciera.gov.co",
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
                REDIS_PASSWORD="RedisPasswordSeguroProductivo2026#$",
                REDIS_SSL=True
            )
        self.assertIn("SFC_URL_BASE", str(ctx.exception))

    def test_crm_webhook_url_http_rechazado_por_defecto(self):
        """
        P1-06: CRM_WEBHOOK_URL en http:// debe rechazarse por defecto -- sin
        CRM_WEBHOOK_ALLOW_INSECURE_HTTP declarado explícitamente, el default seguro
        (exigir https://) sigue aplicando.
        """
        with self.assertRaises(ValidationError) as ctx:
            Settings(
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
                CRM_WEBHOOK_URL="http://crm-interno.internal/api/v1/webhooks/sfc",
                CRM_WEBHOOK_API_KEY="wh_prod_key_999888777",
                SMTP_USER="alertas_prod@global66.com",
                SMTP_PASSWORD="SmtpPasswordSegura2026!",
                ALERT_NOTIFY_EMAILS="ops@global66.com",
                REDIS_PASSWORD="RedisPasswordSeguroProductivo2026#$",
                REDIS_SSL=True
            )
        self.assertIn("CRM_WEBHOOK_URL", str(ctx.exception))
        self.assertIn("CRM_WEBHOOK_ALLOW_INSECURE_HTTP", str(ctx.exception))

    def test_crm_webhook_url_http_permitido_con_flag_explicito(self):
        """
        P1-06: si CRM_WEBHOOK_ALLOW_INSECURE_HTTP=true declara explícitamente que el
        webhook es un endpoint interno de confianza (misma VPC/cuenta AWS), http://
        debe permitirse.
        """
        cfg_prod = Settings(
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
            CRM_WEBHOOK_URL="http://crm-interno.internal/api/v1/webhooks/sfc",
            CRM_WEBHOOK_API_KEY="wh_prod_key_999888777",
            CRM_WEBHOOK_ALLOW_INSECURE_HTTP=True,
            SMTP_USER="alertas_prod@global66.com",
            SMTP_PASSWORD="SmtpPasswordSegura2026!",
            ALERT_NOTIFY_EMAILS="ops@global66.com",
            REDIS_HOST="prod-smartsupervision-redis.abc123.use1.cache.amazonaws.com",
            REDIS_PASSWORD="RedisPasswordSeguroProductivo2026#$",
            REDIS_SSL=True
        )
        self.assertEqual(cfg_prod.CRM_WEBHOOK_URL, "http://crm-interno.internal/api/v1/webhooks/sfc")

    def test_redis_host_vacio_rechazado_en_ci(self):
        """
        P1-04 residual (auditoría adversarial v10): el renderer ya no puede producir
        un REDIS_HOST vacío, pero Settings necesita su propia defensa por si alguien
        inyecta la variable vacía por otra vía (override manual en la consola de ECS,
        bypass del renderer, etc.) -- con env_ignore_empty=True eso caía en silencio
        al default 'localhost' en vez de fallar el arranque.
        """
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                ENVIRONMENT="ci",
                CRM_CORS_ORIGINS="https://crm.global66.com",
                AWS_S3_BUCKET="ci-global66-smartsupervision-attachments",
                AWS_REGION="us-east-1",
                SFC_URL_BASE="https://qasmart.superfinanciera.gov.co",
                SFC_USERNAME="ci_sfc_user",
                SFC_PASSWORD="PasswordSeguroCi2026#$",
                SFC_SECRET_KEY="clave_hmac_secreta_real_ci_2026",
                CRM_API_KEY="g66_sk_ci_real_key_xyz_987654321",
                ADMIN_API_KEY="g66_sk_ci_admin_real_key_abc_123456789",
                CRM_WEBHOOK_URL="https://crm.global66.com/api/v1/webhooks/sfc",
                CRM_WEBHOOK_API_KEY="wh_ci_key_999888777",
                SMTP_USER="alertas_ci@global66.com",
                SMTP_PASSWORD="SmtpPasswordSeguraCi2026!",
                ALERT_NOTIFY_EMAILS="ops@global66.com",
                REDIS_HOST="",
                REDIS_PASSWORD="RedisPasswordSeguroCi2026#$",
                REDIS_SSL=True
            )
        self.assertIn("REDIS_HOST", str(ctx.exception))

    def test_redis_host_localhost_permitido_en_dev(self):
        """
        P1-04 residual: 'dev' se excluye a propósito de este chequeo (mismo motivo que
        ya excluye REDIS_PASSWORD/REDIS_SSL) -- docker-compose.yml reutiliza
        ENVIRONMENT=dev para el Redis local sin auth/TLS, un uso legítimo distinto del
        ambiente AWS 'dev' real.
        """
        cfg_dev = Settings(
            _env_file=None,
            ENVIRONMENT="dev",
            CRM_CORS_ORIGINS="https://crm.global66.com",
            AWS_S3_BUCKET="dev-global66-smartsupervision-attachments",
            AWS_REGION="us-east-1",
            SFC_URL_BASE="https://qasmart.superfinanciera.gov.co",
            SFC_USERNAME="dev_sfc_user",
            SFC_PASSWORD="PasswordSeguroDev2026#$",
            SFC_SECRET_KEY="clave_hmac_secreta_real_dev_2026",
            CRM_API_KEY="g66_sk_dev_real_key_xyz_987654321",
            ADMIN_API_KEY="g66_sk_dev_admin_real_key_abc_123456789",
            CRM_WEBHOOK_URL="https://crm.global66.com/api/v1/webhooks/sfc",
            CRM_WEBHOOK_API_KEY="wh_dev_key_999888777",
            SMTP_USER="alertas_dev@global66.com",
            SMTP_PASSWORD="SmtpPasswordSeguraDev2026!",
            ALERT_NOTIFY_EMAILS="ops@global66.com",
            REDIS_HOST="localhost"
        )
        self.assertEqual(cfg_dev.REDIS_HOST, "localhost")


if __name__ == "__main__":
    unittest.main()