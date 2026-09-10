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
        # 🟢 FIX (revisión despliegue AWS): el Execution Role ya no está hardcodeado
        # en la plantilla -- ahora lo entrega el IaC central como esta variable.
        os.environ["ECS_EXECUTION_ROLE_ARN"] = "arn:aws:iam::999888777666:role/ecsTaskExecutionRole"
        # 🟢 FIX (auditoría nombres de recursos AWS): mismo tratamiento para el Task
        # Role, el repo ECR y los log groups de api/worker -- ya no se asume ningún
        # nombre fijo, todos llegan como Variable de GitHub.
        os.environ["ECS_TASK_ROLE_ARN"] = "arn:aws:iam::999888777666:role/msSmartsupervisionTaskRole-dev"
        os.environ["ECR_REPOSITORY_NAME"] = "ms-integracion-smartsupervision"
        os.environ["ECS_LOG_GROUP_API"] = "/ecs/ms-smartsupervision-dev-api"
        os.environ["ECS_LOG_GROUP_WORKER"] = "/ecs/ms-smartsupervision-dev-worker"
        # 🟢 FIX (auditoría nombres de recursos AWS): el nombre del secreto compuesto
        # ("{environment}/smartsupervision/app-secrets") también se reconstruía a
        # partir de una convención asumida por este repo -- ahora llega ya resuelto.
        os.environ["SECRETS_MANAGER_SECRET_NAME"] = "dev/smartsupervision/app-secrets"

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
            "ECS_EXECUTION_ROLE_ARN": "arn:aws:iam::112233445566:role/ecsTaskExecutionRole",
            "ECS_TASK_ROLE_ARN": "arn:aws:iam::112233445566:role/msSmartsupervisionTaskRole-dev",
            "ECR_REPOSITORY_NAME": "ms-integracion-smartsupervision",
            "ECS_LOG_GROUP_API": "/ecs/ms-smartsupervision-dev-api",
            "ECS_LOG_GROUP_WORKER": "/ecs/ms-smartsupervision-dev-worker",
            "SECRETS_MANAGER_SECRET_NAME": "dev/smartsupervision/app-secrets",
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

    def test_task_definitions_validan_contra_shape_ecs_register_task_definition(self):
        """
        P1-02 (auditoría adversarial v10): render_task_def.py sólo corría en el job
        `deploy` manual -- una regresión del template/renderer que produjera una
        Task Definition estructuralmente inválida para ECS (tipo de campo
        equivocado, estructura que RegisterTaskDefinition rechazaría) podía
        mergearse con tests en verde. Esto valida ambos servicios contra el shape
        real de la operación (mismo chequeo que el auditor hizo a mano con
        botocore.validate.ParamValidator), corriendo en cada push/PR junto al
        resto de la suite.
        """
        from botocore.session import get_session
        from botocore.validate import ParamValidator

        session = get_session()
        operation_model = session.get_service_model("ecs").operation_model("RegisterTaskDefinition")
        validator = ParamValidator()

        for service_type in ["api", "worker"]:
            with self.subTest(service_type=service_type):
                task_def = render_task_definition(service_type, "dev")
                report = validator.validate(task_def, operation_model.input_shape)
                self.assertFalse(
                    report.has_errors(),
                    f"Task Definition de '{service_type}' inválida para RegisterTaskDefinition:\n"
                    f"{report.generate_report()}"
                )

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

    def test_recursos_aws_llegan_100_por_ciento_de_variables_externas(self):
        """
        🟢 FIX (auditoría nombres de recursos AWS): taskRoleArn, el repo ECR, el log
        group y el secreto compuesto de Secrets Manager estaban hardcodeados en la
        plantilla/script asumiendo la convención de nombres del IaC central
        ("msSmartsupervisionTaskRole-${ENVIRONMENT}", "ms-integracion-
        smartsupervision", "/ecs/ms-smartsupervision-${ENVIRONMENT}-${SERVICE_TYPE}",
        "{ENVIRONMENT}/smartsupervision/app-secrets") -- un desalineamiento con el
        nombre real sólo se descubría a mitad de un despliegue. Verifica que el
        valor renderizado sea EXACTAMENTE el que trae la Variable de entorno (no un
        patrón reconstruido por este repo), y que el log group correcto se elija
        según el service_type.
        """
        os.environ["ECS_TASK_ROLE_ARN"] = "arn:aws:iam::999888777666:role/nombre-real-que-decide-terraform"
        os.environ["ECR_REPOSITORY_NAME"] = "repo-ecr-real-del-equipo"
        os.environ["ECS_LOG_GROUP_API"] = "/log-group-real-api"
        os.environ["ECS_LOG_GROUP_WORKER"] = "/log-group-real-worker"
        os.environ["SECRETS_MANAGER_SECRET_NAME"] = "nombre-real-de-secreto-que-decide-terraform"
        del os.environ["SECRET_SUFFIX"]

        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value.describe_secret.return_value = {
                "ARN": "arn:aws:secretsmanager:us-east-1:999888777666:secret:nombre-real-de-secreto-que-decide-terraform-XyZ123"
            }
            result_api = render_task_definition("api", "dev")
            mock_boto_client.return_value.describe_secret.assert_called_once_with(
                SecretId="nombre-real-de-secreto-que-decide-terraform"
            )
            result_worker = render_task_definition("worker", "dev")

        self.assertEqual(
            result_api["taskRoleArn"],
            "arn:aws:iam::999888777666:role/nombre-real-que-decide-terraform",
        )
        self.assertIn("repo-ecr-real-del-equipo", result_api["containerDefinitions"][0]["image"])
        self.assertEqual(
            result_api["containerDefinitions"][0]["logConfiguration"]["options"]["awslogs-group"],
            "/log-group-real-api",
        )
        crm_secret = result_api["containerDefinitions"][0]["secrets"][0]["valueFrom"]
        self.assertIn("nombre-real-de-secreto-que-decide-terraform-XyZ123", crm_secret)

        self.assertEqual(
            result_worker["containerDefinitions"][0]["logConfiguration"]["options"]["awslogs-group"],
            "/log-group-real-worker",
        )

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

    def test_missing_redis_host_fails_fast(self):
        """
        Tras el feedback de infraestructura (SSV corre dentro del cluster/ALB
        compartidos de CRM Global66, no recursos dedicados), REDIS_HOST volvió a
        ser una variable de entorno simple -- sin resolución dinámica contra
        ElastiCache. Sin ella, el render debe fallar con un ValueError claro.
        """
        if "REDIS_HOST" in os.environ:
            del os.environ["REDIS_HOST"]

        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
        del env_test["REDIS_HOST"]

        with patch.dict(os.environ, env_test, clear=True):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")

        self.assertIn("REDIS_HOST", str(ctx.exception))

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

    def test_missing_ecs_execution_role_arn_fails_fast(self):
        """
        El Execution Role ya no se hardcodea en la plantilla (antes
        'ecsTaskExecutionRole' fijo) -- debe llegar como Variable de GitHub por
        ambiente. Sin ella, el render debe fallar en vez de dejar el placeholder
        sin resolver (RENDER ERROR genérico) o, peor, resolver a un nombre fijo.
        """
        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
        del env_test["ECS_EXECUTION_ROLE_ARN"]

        with patch.dict(os.environ, env_test, clear=True):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")

        self.assertIn("ECS_EXECUTION_ROLE_ARN", str(ctx.exception))

    def test_missing_ecs_task_role_arn_fails_fast(self):
        """
        El Task Role tenía el mismo problema que ya se corrigió para el Execution
        Role: estaba hardcodeado en la plantilla asumiendo un nombre
        ("msSmartsupervisionTaskRole-${ENVIRONMENT}") que el IaC central debía
        adivinar. Ahora es una Variable obligatoria -- sin ella, el render debe
        fallar en vez de resolver a un nombre fijo.
        """
        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
        del env_test["ECS_TASK_ROLE_ARN"]

        with patch.dict(os.environ, env_test, clear=True):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")

        self.assertIn("ECS_TASK_ROLE_ARN", str(ctx.exception))

    def test_missing_ecr_repository_name_fails_fast(self):
        """
        El nombre del repositorio ECR estaba hardcodeado ("ms-integracion-
        smartsupervision") tanto en la plantilla como en el workflow -- sin la
        Variable, el render debe fallar en vez de asumir ese nombre.
        """
        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
        del env_test["ECR_REPOSITORY_NAME"]

        with patch.dict(os.environ, env_test, clear=True):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")

        self.assertIn("ECR_REPOSITORY_NAME", str(ctx.exception))

    def test_missing_ecs_log_group_fails_fast(self):
        """
        Los log groups de api/worker estaban hardcodeados en la plantilla
        ("/ecs/ms-smartsupervision-${ENVIRONMENT}-${SERVICE_TYPE}") asumiendo que
        Terraform los crearía con ese nombre exacto -- sin verificación previa, sólo
        se descubría un desalineamiento cuando la tarea fallaba al arrancar. Ambas
        Variables (API y WORKER) son obligatorias para cualquier render, sin
        importar qué service_type se esté renderizando.
        """
        for var_faltante in ["ECS_LOG_GROUP_API", "ECS_LOG_GROUP_WORKER"]:
            with self.subTest(var_faltante=var_faltante):
                env_test = self.env_vars.copy()
                env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
                del env_test[var_faltante]

                with patch.dict(os.environ, env_test, clear=True):
                    with self.assertRaises(ValueError) as ctx:
                        render_task_definition("api", "dev")

                self.assertIn(var_faltante, str(ctx.exception))

    def test_missing_secrets_manager_secret_name_fails_fast(self):
        """
        El nombre del secreto compuesto de Secrets Manager
        ("{environment}/smartsupervision/app-secrets") se reconstruía dentro de
        _resolver_secret_suffix a partir de esa convención asumida por este repo --
        ahora es una Variable obligatoria y ya no se reconstruye en ningún lado.
        """
        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
        del env_test["SECRETS_MANAGER_SECRET_NAME"]

        with patch.dict(os.environ, env_test, clear=True):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "dev")

        self.assertIn("SECRETS_MANAGER_SECRET_NAME", str(ctx.exception))

    def test_infra_critica_faltante_falla_tambien_fuera_de_prod(self):
        """
        Tras el feedback de infraestructura para el despliegue en CI, el fail-fast de
        estas variables ya no se limita a 'prod' -- un despliegue de prueba en
        'ci'/'dev' con infraestructura equivocada es igual de silencioso y peligroso.
        """
        env_test = self.env_vars.copy()
        env_test["IMAGE_TAG"] = "git-commit-a1b2c3d4e5f6"
        del env_test["SFC_URL_BASE"]

        with patch.dict(os.environ, env_test, clear=True):
            with self.assertRaises(ValueError) as ctx:
                render_task_definition("api", "ci")

            self.assertIn("SFC_URL_BASE", str(ctx.exception))

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