import unittest
from pydantic import ValidationError
from app.core.config import Settings


class TestCorsSecurityValidation(unittest.TestCase):

    def setUp(self):
        self.valid_env_data = {
            "_env_file": None,
            "AWS_S3_BUCKET": "test-bucket",
            "AWS_REGION": "us-east-1",
            "SFC_URL_BASE": "https://smart.superfinanciera.gov.co",
            "SFC_USERNAME": "usr_real",
            "SFC_PASSWORD": "PasswordSeguro2026!",
            "SFC_SECRET_KEY": "hmac_key_segura_2026",
            "CRM_API_KEY": "crm_key_segura_2026",
            "ADMIN_API_KEY": "admin_key_segura_2026",
            "CRM_WEBHOOK_URL": "https://crm.global66.com/webhook",
            "SMTP_USER": "alertas@global66.com",
            "SMTP_PASSWORD": "SmtpPasswordSegura2026!",
            "ALERT_NOTIFY_EMAILS": "ops@global66.com"
        }

    def test_cors_wildcard_rechazado_en_ambientes_estrictos(self):
        """
        Verifica que el comodín '*' en CRM_CORS_ORIGINS lance ValidationError
        en entornos CI, Dev, QA y Producción.
        """
        ambientes = ["ci", "dev", "qa", "staging", "production"]
        
        for env in ambientes:
            with self.subTest(environment=env):
                data = self.valid_env_data.copy()
                data["ENVIRONMENT"] = env
                data["CRM_CORS_ORIGINS"] = "*"

                with self.assertRaises(ValidationError) as ctx:
                    Settings(**data)

                self.assertIn("contiene el comodín '*'", str(ctx.exception))

    def test_cors_dominios_explicitos_permitidos(self):
        """Verifica que dominios HTTPS explícitos sean aceptados correctamente."""
        data = self.valid_env_data.copy()
        data["ENVIRONMENT"] = "qa"
        data["CRM_CORS_ORIGINS"] = "https://crm-qa.global66.com,https://admin-qa.global66.com"

        cfg = Settings(**data)
        self.assertEqual(len(cfg.CRM_CORS_ORIGINS), 2)
        self.assertIn("https://crm-qa.global66.com", cfg.CRM_CORS_ORIGINS)

    def test_cors_wildcard_permitido_en_local(self):
        """Verifica que el comodín '*' sea permitido únicamente cuando ENVIRONMENT='local'."""
        data = self.valid_env_data.copy()
        data["ENVIRONMENT"] = "local"
        data["CRM_CORS_ORIGINS"] = "*"

        cfg = Settings(**data)
        self.assertEqual(cfg.CRM_CORS_ORIGINS, ["*"])


if __name__ == "__main__":
    unittest.main()