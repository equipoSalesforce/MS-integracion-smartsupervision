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
        os.environ["SMTP_FROM_EMAIL"] = "alertas@global66.com"
        # 🟢 FIX (revisión despliegue AWS): obligatorias en 'prod' desde que
        # render_task_def.py dejó de permitir sus fallbacks silenciosos ahí.
        os.environ["SFC_URL_BASE"] = "https://smart.superfinanciera.gov.co"
        os.environ["CRM_CORS_ORIGINS"] = "https://crm.global66.com"
        os.environ["REDIS_HOST"] = "prod-smartsupervision-redis.cache.amazonaws.com"
        os.environ["AWS_S3_BUCKET"] = "prod-global66-smartsupervision-attachments"
        os.environ["GOOGLE_SPREADSHEET_ID"] = "1a2b3c4d5e6f7g8h9i0j"
        os.environ["GOOGLE_CATALOGS_SPREADSHEET_ID"] = "0j9i8h7g6f5e4d3c2b1a"

        self.env_vars = {
            "AWS_ACCOUNT_ID": "112233445566",
            "SECRET_SUFFIX": "a1b2c3",
            "AWS_REGION": "us-east-1",
            "SMTP_FROM_EMAIL": "alertas@global66.com",
            "SFC_URL_BASE": "https://smart.superfinanciera.gov.co",
            "CRM_CORS_ORIGINS": "https://crm.global66.com",
            "REDIS_HOST": "prod-smartsupervision-redis.cache.amazonaws.com",
            "AWS_S3_BUCKET": "prod-global66-smartsupervision-attachments",
            "GOOGLE_SPREADSHEET_ID": "1a2b3c4d5e6f7g8h9i0j",
            "GOOGLE_CATALOGS_SPREADSHEET_ID": "0j9i8h7g6f5e4d3c2b1a",
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

    def test_secret_suffix_sin_override_falla_si_secrets_manager_no_responde(self):
        """
        Sin SECRET_SUFFIX como override manual, el render debe intentar resolver el
        sufijo real contra Secrets Manager (secretsmanager:DescribeSecret) -- y fallar
        con un ValueError claro si esa consulta falla (secreto inexistente, sin
        permisos, etc.), en vez de continuar con un ARN roto.
        """
        if "SECRET_SUFFIX" in os.environ:
            del os.environ["SECRET_SUFFIX"]

        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value.describe_secret.side_effect = Exception(
                "ResourceNotFoundException: secret not found"
            )
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")

        self.assertIn("Secrets Manager", str(ctx.exception))

    def test_secret_suffix_sin_override_resuelve_via_secrets_manager(self):
        """
        Sin SECRET_SUFFIX, el render debe resolver el sufijo real consultando el ARN
        del secreto en Secrets Manager -- así nadie tiene que copiarlo a mano después
        de que el plan de Terraform cree el secreto.
        """
        if "SECRET_SUFFIX" in os.environ:
            del os.environ["SECRET_SUFFIX"]

        arn_falso = "arn:aws:secretsmanager:us-east-1:999888777666:secret:dev/smartsupervision/app-secrets-XyZ123"
        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value.describe_secret.return_value = {"ARN": arn_falso}
            result = render_task_definition("api", "dev")

        mock_boto_client.return_value.describe_secret.assert_called_once_with(
            SecretId="dev/smartsupervision/app-secrets"
        )
        crm_secret = result["containerDefinitions"][0]["secrets"][0]["valueFrom"]
        self.assertIn("XyZ123", crm_secret)
        self.assertNotIn("??????", crm_secret)

    def test_redis_host_sin_override_resuelve_via_elasticache(self):
        """
        Sin REDIS_HOST, el render debe resolver el endpoint real consultando
        ElastiCache -- así nadie tiene que copiar el host a mano a una GitHub
        Variable después de que Terraform cree el replication group.
        """
        if "REDIS_HOST" in os.environ:
            del os.environ["REDIS_HOST"]

        endpoint_falso = "smartsupervision-dev.abc123.0001.use1.cache.amazonaws.com"
        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value.describe_replication_groups.return_value = {
                "ReplicationGroups": [{
                    "NodeGroups": [{"PrimaryEndpoint": {"Address": endpoint_falso}}]
                }]
            }
            result = render_task_definition("api", "dev")

        mock_boto_client.return_value.describe_replication_groups.assert_called_once_with(
            ReplicationGroupId="smartsupervision-dev"
        )
        env_vars = {
            e["name"]: e["value"]
            for e in result["containerDefinitions"][0]["environment"]
        }
        self.assertEqual(env_vars["REDIS_HOST"], endpoint_falso)

    def test_redis_host_cluster_mode_usa_configuration_endpoint(self):
        """
        Con REDIS_CLUSTER_MODE=True, el host debe resolverse desde el
        ConfigurationEndpoint (Cluster Mode Enabled), no del NodeGroup primario
        -- aioredis.RedisCluster necesita ese endpoint, no el de un solo shard.
        """
        if "REDIS_HOST" in os.environ:
            del os.environ["REDIS_HOST"]

        endpoint_falso = "smartsupervision-prod.abc123.clustercfg.use1.cache.amazonaws.com"
        with patch.dict(os.environ, {"REDIS_CLUSTER_MODE": "True"}):
            with patch("boto3.client") as mock_boto_client:
                mock_boto_client.return_value.describe_replication_groups.return_value = {
                    "ReplicationGroups": [{
                        "ConfigurationEndpoint": {"Address": endpoint_falso}
                    }]
                }
                result = render_task_definition("api", "dev")

        env_vars = {
            e["name"]: e["value"]
            for e in result["containerDefinitions"][0]["environment"]
        }
        self.assertEqual(env_vars["REDIS_HOST"], endpoint_falso)
        self.assertEqual(env_vars["REDIS_CLUSTER_MODE"], "True")

    def test_redis_host_sin_override_falla_si_elasticache_no_responde(self):
        """
        Sin REDIS_HOST y sin poder resolver el replication group en ElastiCache
        (no existe, sin permisos, etc.), el render debe fallar con un ValueError
        claro en vez de continuar con un host roto o inventado.
        """
        if "REDIS_HOST" in os.environ:
            del os.environ["REDIS_HOST"]

        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value.describe_replication_groups.side_effect = Exception(
                "ReplicationGroupNotFoundFault: replication group not found"
            )
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")

        self.assertIn("ElastiCache", str(ctx.exception))

    def test_missing_image_tag_fails_fast(self):
        """Verifica que se lance un ValueError si IMAGE_TAG no existe en el entorno."""
        if "IMAGE_TAG" in os.environ:
            del os.environ["IMAGE_TAG"]

        with self.assertRaises(ValueError) as ctx:
            render_task_definition("api", "dev")

        self.assertIn("IMAGE_TAG", str(ctx.exception))

    def test_missing_smtp_from_email_fails_fast(self):
        """
        P1-08: SMTP_FROM_EMAIL es obligatoria para el render de AWS — sin ella, la app
        caería de vuelta a SMTP_USER como remitente y las alertas fallarían en SES
        silenciosamente. Verifica que se lance un ValueError si no está presente.
        """
        if "SMTP_FROM_EMAIL" in os.environ:
            del os.environ["SMTP_FROM_EMAIL"]

        with self.assertRaises(ValueError) as ctx:
            render_task_definition("api", "dev")

        self.assertIn("SMTP_FROM_EMAIL", str(ctx.exception))

    def test_infra_critica_faltante_falla_en_prod(self):
        """
        Verifica que en 'prod' se rechace el render si falta alguna variable crítica de
        infraestructura (SFC_URL_BASE, REDIS_HOST, AWS_S3_BUCKET, etc.) en vez de caer
        en silencio a sus valores por defecto (que apuntan a QA/ejemplo).
        """
        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
        del env_test["SFC_URL_BASE"]

        with patch.dict(os.environ, env_test, clear=True):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "prod")

            self.assertIn("SFC_URL_BASE", str(ctx.exception))

    def test_infra_critica_faltante_solo_advierte_fuera_de_prod(self):
        """
        Fuera de 'prod' (ej. un despliegue de prueba a 'ci'/'dev'), la ausencia de estas
        variables no debe bloquear el render -- solo se advierte por consola.
        """
        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
        del env_test["SFC_URL_BASE"]

        with patch.dict(os.environ, env_test, clear=True):
            result = render_task_definition("api", "ci")
            self.assertIsInstance(result, dict)

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