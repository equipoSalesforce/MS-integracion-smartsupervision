# tests/test_render_task_def.py
import os
import unittest
from unittest.mock import patch

try:
    from scripts.render_task_def import render_task_definition
except ImportError:
    from render_task_def import render_task_definition


class TestRenderTaskDefinition(unittest.TestCase):

    def setUp(self):
        # 🟢 Variables requeridas globalmente para los happy paths de prueba
        os.environ["AWS_ACCOUNT_ID"] = "999888777666"
        os.environ["SECRET_SUFFIX"] = "a1b2c3"
        os.environ["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
        
        self.env_vars = {
            "AWS_ACCOUNT_ID": "112233445566",
            "SECRET_SUFFIX": "a1b2c3",
            "AWS_REGION": "us-east-1"
        }

    def tearDown(self):
        # 🧹 Limpieza de archivos JSON temporales generados durante las pruebas
        for srv in ["api", "worker"]:
            for env in ["dev", "qa", "prod", "ci"]:
                fname = f"ecs-task-def-{srv}-{env}.json"
                if os.path.exists(fname):
                    try:
                        os.remove(fname)
                    except OSError:
                        pass

    def test_render_task_definition_exito(self):
        """Verifica el renderizado exitoso en todas las combinaciones cuando las variables están presentes."""
        for service_type in ["api", "worker"]:
            for environment in ["dev", "qa", "prod"]:
                with self.subTest(service_type=service_type, environment=environment):
                    result = render_task_definition(service_type, environment)
                    self.assertIsInstance(result, dict)
                    
                    # Aserta que el ARN del secreto haya sido renderizado limpiamente con el sufijo real
                    crm_secret = result["containerDefinitions"][0]["secrets"][0]["valueFrom"]
                    self.assertNotIn("??????", crm_secret)
                    self.assertIn("a1b2c3", crm_secret)

    def test_crm_cors_origins_formato_lista_json_real_no_rompe_el_render(self):
        """
        Auditoría 2026-08-13 (P0-03): CRM_CORS_ORIGINS en el formato de lista JSON que
        .env.example documenta (comillas embebidas) debía producir un JSONDecodeError
        antes de este fix. Reproduce exactamente el repro de la auditoría para API y
        worker, y verifica que el valor sobreviva íntegro en el JSON final.
        """
        valor_real = '["https://crm.global66.com", "http://localhost:3000"]'
        with patch.dict(os.environ, {"CRM_CORS_ORIGINS": valor_real}):
            for service_type in ["api", "worker"]:
                with self.subTest(service_type=service_type):
                    result = render_task_definition(service_type, "ci")
                    env_vars = {
                        e["name"]: e["value"]
                        for e in result["containerDefinitions"][0]["environment"]
                    }
                    self.assertEqual(env_vars["CRM_CORS_ORIGINS"], valor_real)

    def test_missing_aws_account_id_fails_fast(self):
        """Verifica que se lance un ValueError si AWS_ACCOUNT_ID no existe o es ficticia."""
        if "AWS_ACCOUNT_ID" in os.environ:
            del os.environ["AWS_ACCOUNT_ID"]

        with self.assertRaises(ValueError) as ctx:
            render_task_definition("api", "dev")

        self.assertIn("AWS_ACCOUNT_ID", str(ctx.exception))

    def test_missing_secret_suffix_fails_fast(self):
        """Verifica que se lance un ValueError si SECRET_SUFFIX no existe o es '??????'."""
        if "SECRET_SUFFIX" in os.environ:
            del os.environ["SECRET_SUFFIX"]

        with self.assertRaises(ValueError) as ctx:
            render_task_definition("api", "dev")

        self.assertIn("SECRET_SUFFIX", str(ctx.exception))

    def test_missing_image_tag_fails_fast(self):
        """Verifica que se lance un ValueError si IMAGE_TAG no existe en el entorno."""
        if "IMAGE_TAG" in os.environ:
            del os.environ["IMAGE_TAG"]

        with self.assertRaises(ValueError) as ctx:
            render_task_definition("api", "dev")

        self.assertIn("IMAGE_TAG", str(ctx.exception))

    def test_unrendered_placeholder_fails(self):
        """Verifica que el script falle si queda algún placeholder ${...} sin reemplazar."""
        plantilla_con_variable_huerfana = '{"family": "test", "dummy": "${VARIABLE_HUERFANA}"}'
        
        with patch("builtins.open", unittest.mock.mock_open(read_data=plantilla_con_variable_huerfana)):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")
            
            self.assertIn("VARIABLE_HUERFANA", str(ctx.exception))
            
    def test_image_tag_latest_lanza_error(self):
        """Verifica que si IMAGE_TAG es 'latest', el renderizador aborte con un ValueError."""
        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "latest"

        with patch.dict(os.environ, env_test, clear=True):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")

            self.assertIn("IMAGE_TAG", str(ctx.exception))
            self.assertIn("latest", str(ctx.exception))

    def test_image_tag_ausente_lanza_error(self):
        """Verifica que si IMAGE_TAG no se proporciona, el renderizador aborte con un ValueError."""
        with patch.dict(os.environ, self.env_vars, clear=True):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")

            self.assertIn("IMAGE_TAG", str(ctx.exception))

    def test_image_tag_inmutable_sha_exito(self):
        """Verifica que un Commit SHA de Git sea aceptado correctamente."""
        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"

        with patch.dict(os.environ, env_test, clear=True):
            task_def = render_task_definition("api", "dev")
            image_uri = task_def["containerDefinitions"][0]["image"]
            self.assertTrue(image_uri.endswith(":git-commit-a1b2c3d4e5f6"))


if __name__ == "__main__":
    unittest.main()