# tests/test_run_post_deploy_smoke.py
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

try:
    from scripts.run_post_deploy_smoke import main
except ImportError:
    from run_post_deploy_smoke import main


def _run_main(environment):
    old_argv = sys.argv
    sys.argv = ["run_post_deploy_smoke.py", environment]
    try:
        return main()
    finally:
        sys.argv = old_argv


class TestRunPostDeploySmoke(unittest.TestCase):

    def setUp(self):
        self.env = {
            "AWS_REGION": "us-east-1",
            "ECS_CLUSTER_NAME": "global66-crm-b2c-ci-cluster",
            "SSV_SMOKE_BASE_URL": "https://ci.cora.global66.com",
            "SMOKE_TASK_DEFINITION_ARN": "arn:aws:ecs:us-east-1:999888777666:task-definition/ms-smartsupervision-api-dev:7",
            "ECS_SUBNET_IDS": '["subnet-0123456789abcdef0", "subnet-0abc123def4567890"]',
            "ECS_SECURITY_GROUP_IDS": '["sg-0123456789abcdef0"]',
        }

    @patch("scripts.run_post_deploy_smoke.boto3.client")
    def test_exito_task_exit_code_cero(self, mock_boto_client):
        mock_ecs = MagicMock()
        mock_boto_client.return_value = mock_ecs

        mock_ecs.run_task.return_value = {
            "failures": [],
            "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:999888777666:task/global66-crm-b2c-ci-cluster/abc123"}],
        }
        mock_ecs.get_waiter.return_value = MagicMock()
        mock_ecs.describe_tasks.return_value = {
            "tasks": [{"containers": [{"exitCode": 0, "reason": None}]}]
        }

        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(_run_main("ci"), 0)

        mock_boto_client.assert_called_once_with("ecs", region_name="us-east-1")
        run_task_kwargs = mock_ecs.run_task.call_args.kwargs
        self.assertEqual(run_task_kwargs["cluster"], "global66-crm-b2c-ci-cluster")
        self.assertEqual(
            run_task_kwargs["networkConfiguration"]["awsvpcConfiguration"]["subnets"],
            ["subnet-0123456789abcdef0", "subnet-0abc123def4567890"],
        )
        self.assertEqual(
            run_task_kwargs["overrides"]["containerOverrides"][0]["environment"][0],
            {"name": "SMOKE_ALB_BASE_URL", "value": "https://ci.cora.global66.com"},
        )

    @patch("scripts.run_post_deploy_smoke.boto3.client")
    def test_falla_si_exit_code_no_es_cero(self, mock_boto_client):
        mock_ecs = MagicMock()
        mock_boto_client.return_value = mock_ecs

        mock_ecs.run_task.return_value = {
            "failures": [],
            "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:999888777666:task/global66-crm-b2c-ci-cluster/abc123"}],
        }
        mock_ecs.get_waiter.return_value = MagicMock()
        mock_ecs.describe_tasks.return_value = {
            "tasks": [{"containers": [{"exitCode": 1, "reason": "smoke check failed"}]}]
        }

        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(_run_main("ci"), 1)

    @patch("scripts.run_post_deploy_smoke.boto3.client")
    def test_falla_si_run_task_reporta_failures(self, mock_boto_client):
        mock_ecs = MagicMock()
        mock_boto_client.return_value = mock_ecs

        mock_ecs.run_task.return_value = {
            "failures": [{"reason": "RESOURCE:FARGATE", "arn": "..."}],
            "tasks": [],
        }

        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(_run_main("ci"), 1)

        mock_ecs.get_waiter.assert_not_called()

    def test_falla_fast_si_falta_cluster_name(self):
        env = dict(self.env)
        del env["ECS_CLUSTER_NAME"]
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_run_main("ci"), 1)

    def test_falla_fast_si_falta_smoke_base_url(self):
        env = dict(self.env)
        del env["SSV_SMOKE_BASE_URL"]
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_run_main("ci"), 1)

    def test_falla_fast_si_falta_task_definition_arn(self):
        env = dict(self.env)
        del env["SMOKE_TASK_DEFINITION_ARN"]
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_run_main("ci"), 1)

    def test_falla_fast_si_subnets_no_son_json_valido(self):
        env = dict(self.env)
        env["ECS_SUBNET_IDS"] = "subnet-0123456789abcdef0"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_run_main("ci"), 1)

    def test_falla_fast_sin_argumento_environment(self):
        old_argv = sys.argv
        sys.argv = ["run_post_deploy_smoke.py"]
        try:
            self.assertEqual(main(), 1)
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
