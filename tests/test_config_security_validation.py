import unittest
from pydantic import ValidationError
from app.core.config import Settings


class TestConfigSecurityValidation(unittest.TestCase):

    def test_startup_exitoso_en_ambiente_local_con_defaults(self):
        """
        Verifica que en ambiente 'local' o 'development' el microservicio permita
        los valores por defecto para facilitar el desarrollo local.
        """
        cfg_local = Settings(_env_file=None, ENVIRONMENT="local")
        self.assertEqual(cfg_local.ENVIRONMENT, "local")

    def test_startup_fallido_en_produccion_con_defaults_inseguros(self):
        """
        Verifica el principio Fail-Fast: en ambiente 'production' debe lanzar
        ValidationError si no se reemplazaron las claves por defecto de prueba.
        Aísla el test deshabilitando la lectura del .env local.
        """
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                _env_file=None,
                ENVIRONMENT="production",
                CRM_API_KEY="g66_sk_test_super_secreto_12345",
                ADMIN_API_KEY="g66_sk_test_admin_secreto_99999",
                SFC_SECRET_KEY="global66_sfc_secret_key_testing_2026",
                SFC_PASSWORD="123456789"
            )

        error_str = str(ctx.exception)
        self.assertIn("RIESGO CRÍTICO DE SEGURIDAD", error_str)
        self.assertIn("CRM_API_KEY", error_str)
        self.assertIn("SFC_SECRET_KEY", error_str)

    def test_startup_exitoso_en_produccion_con_secretos_reales(self):
        """
        Verifica que en producción el servicio arranque sin problemas cuando
        todas las variables secretas hayan sido inyectadas adecuadamente.
        """
        cfg_prod = Settings(
            _env_file=None,
            ENVIRONMENT="production",
            CRM_API_KEY="g66_sk_prod_real_key_xyz_987654321",
            ADMIN_API_KEY="g66_sk_prod_admin_real_key_abc_123456789",
            SFC_SECRET_KEY="clave_hmac_secreta_real_otorgada_por_sfc_2026",
            SFC_PASSWORD="PasswordSeguroProductivo2026#$"
        )
        self.assertEqual(cfg_prod.ENVIRONMENT, "production")
        self.assertEqual(cfg_prod.CRM_API_KEY, "g66_sk_prod_real_key_xyz_987654321")


if __name__ == "__main__":
    unittest.main()