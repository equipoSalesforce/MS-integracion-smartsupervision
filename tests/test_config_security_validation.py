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
            REDIS_PASSWORD="RedisPasswordSeguroProductivo2026#$",
            REDIS_SSL=True
        )
        self.assertEqual(cfg_prod.ENVIRONMENT, "production")
        self.assertEqual(cfg_prod.CRM_API_KEY, "g66_sk_prod_real_key_xyz_987654321")


if __name__ == "__main__":
    unittest.main()