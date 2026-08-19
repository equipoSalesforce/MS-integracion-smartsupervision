# tests/test_run_post_deploy_smoke.py
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

try:
    from scripts.run_post_deploy_smoke import main
except ImportError:
    from run_post_deploy_smoke import main


def _mock_clients(mock_ecs, mock_elbv2):
    def _side_effect(service_name, **kwargs):
        if service_name == "ecs":
            return mock_ecs
        if service_name == "elbv2":
            return mock_elbv2
        raise ValueError(f"Cliente boto3 inesperado en el test: {service_name}")
    return _side_effect


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
            "SMOKE_TASK_DEFINITION_ARN": "arn:aws:ecs:us-east-1:999888777666:task-definition/ms-smartsupervision-api-dev:7",
            "ECS_SUBNET_IDS": '["subnet-0123456789abcdef0", "subnet-0abc123def4567890"]',
            "ECS_SECURITY_GROUP_IDS": '["sg-0123456789abcdef0"]',
        }

    @patch("scripts.run_post_deploy_smoke.boto3.client")
    def test_exito_task_exit_code_cero(self, mock_boto_client):
        mock_ecs = MagicMock()
        mock_elbv2 = MagicMock()
        mock_boto_client.side_effect = _mock_clients(mock_ecs, mock_elbv2)

        mock_elbv2.describe_load_balancers.return_value = {
            "LoadBalancers": [{"DNSName": "internal-ms-smartsupervision-dev-123.us-east-1.elb.amazonaws.com"}]
        }
        mock_ecs.run_task.return_value = {
            "failures": [],
            "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:999888777666:task/ms-smartsupervision-dev/abc123"}],
        }
        mock_ecs.get_waiter.return_value = MagicMock()
        mock_ecs.describe_tasks.return_value = {
            "tasks": [{"containers": [{"exitCode": 0, "reason": None}]}]
        }

        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(_run_main("dev"), 0)

        mock_elbv2.describe_load_balancers.assert_called_once_with(Names=["ms-smartsupervision-dev"])
        run_task_kwargs = mock_ecs.run_task.call_args.kwargs
        self.assertEqual(run_task_kwargs["cluster"], "ms-smartsupervision-dev")
        self.assertEqual(
            run_task_kwargs["networkConfiguration"]["awsvpcConfiguration"]["subnets"],
            ["subnet-0123456789abcdef0", "subnet-0abc123def4567890"],
        )
        self.assertEqual(
            run_task_kwargs["overrides"]["containerOverrides"][0]["environment"][0],
            {"name": "SMOKE_ALB_BASE_URL", "value": "https://internal-ms-smartsupervision-dev-123.us-east-1.elb.amazonaws.com"},
        )

    @patch("scripts.run_post_deploy_smoke.boto3.client")
    def test_falla_si_exit_code_no_es_cero(self, mock_boto_client):
        mock_ecs = MagicMock()
        mock_elbv2 = MagicMock()
        mock_boto_client.side_effect = _mock_clients(mock_ecs, mock_elbv2)

        mock_elbv2.describe_load_balancers.return_value = {"LoadBalancers": [{"DNSName": "alb.internal"}]}
        mock_ecs.run_task.return_value = {
            "failures": [],
            "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:999888777666:task/ms-smartsupervision-dev/abc123"}],
        }
        mock_ecs.get_waiter.return_value = MagicMock()
        mock_ecs.describe_tasks.return_value = {
            "tasks": [{"containers": [{"exitCode": 1, "reason": "smoke check failed"}]}]
        }

        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(_run_main("dev"), 1)

    @patch("scripts.run_post_deploy_smoke.boto3.client")
    def test_falla_si_run_task_reporta_failures(self, mock_boto_client):
        mock_ecs = MagicMock()
        mock_elbv2 = MagicMock()
        mock_boto_client.side_effect = _mock_clients(mock_ecs, mock_elbv2)

        mock_elbv2.describe_load_balancers.return_value = {"LoadBalancers": [{"DNSName": "alb.internal"}]}
        mock_ecs.run_task.return_value = {
            "failures": [{"reason": "RESOURCE:FARGATE", "arn": "..."}],
            "tasks": [],
        }

        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(_run_main("dev"), 1)

        mock_ecs.get_waiter.assert_not_called()

    @patch("scripts.run_post_deploy_smoke.boto3.client")
    def test_falla_si_no_se_puede_resolver_el_alb(self, mock_boto_client):
        mock_ecs = MagicMock()
        mock_elbv2 = MagicMock()
        mock_boto_client.side_effect = _mock_clients(mock_ecs, mock_elbv2)
        mock_elbv2.describe_load_balancers.side_effect = Exception("LoadBalancerNotFoundException")

        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(_run_main("dev"), 1)

        mock_ecs.run_task.assert_not_called()

    def test_falla_fast_si_falta_task_definition_arn(self):
        env = dict(self.env)
        del env["SMOKE_TASK_DEFINITION_ARN"]
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_run_main("dev"), 1)

    def test_falla_fast_si_subnets_no_son_json_valido(self):
        env = dict(self.env)
        env["ECS_SUBNET_IDS"] = "subnet-0123456789abcdef0"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_run_main("dev"), 1)

    def test_falla_fast_sin_argumento_environment(self):
        old_argv = sys.argv
        sys.argv = ["run_post_deploy_smoke.py"]
        try:
            self.assertEqual(main(), 1)
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
