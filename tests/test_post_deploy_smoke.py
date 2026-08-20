# tests/test_post_deploy_smoke.py
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

try:
    from scripts.post_deploy_smoke import main, verificar_redis_via_alb, verificar_s3
except ImportError:
    from post_deploy_smoke import main, verificar_redis_via_alb, verificar_s3


class TestPostDeploySmoke(unittest.TestCase):

    @patch("scripts.post_deploy_smoke.httpx.get")
    def test_verificar_redis_ok(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"servicio": "SSV", "redis": True}
        )
        self.assertTrue(verificar_redis_via_alb("https://alb.internal"))
        mock_get.assert_called_once_with(
            "https://alb.internal/api/v1/quejas/_health/ready", timeout=10.0
        )

    @patch("scripts.post_deploy_smoke.httpx.get")
    def test_verificar_redis_unhealthy_status_code(self, mock_get):
        mock_get.return_value = MagicMock(status_code=503, text="unhealthy")
        self.assertFalse(verificar_redis_via_alb("https://alb.internal"))

    @patch("scripts.post_deploy_smoke.httpx.get")
    def test_verificar_redis_falla_si_servicio_no_es_ssv(self, mock_get):
        """
        200 OK pero de otro servicio detrás del ALB compartido con CRM (routing
        ambiguo) -- no debe aceptarse como éxito del smoke de SSV.
        """
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"servicio": "OTRO_SERVICIO", "redis": True}
        )
        self.assertFalse(verificar_redis_via_alb("https://alb.internal"))

    @patch("scripts.post_deploy_smoke.httpx.get")
    def test_verificar_redis_falla_si_redis_false(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"servicio": "SSV", "redis": False}
        )
        self.assertFalse(verificar_redis_via_alb("https://alb.internal"))

    @patch("scripts.post_deploy_smoke.httpx.get")
    def test_verificar_redis_falla_si_body_no_es_json(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, json=MagicMock(side_effect=ValueError("bad json")), text="not json"
        )
        self.assertFalse(verificar_redis_via_alb("https://alb.internal"))

    @patch("scripts.post_deploy_smoke.httpx.get", side_effect=Exception("timeout"))
    def test_verificar_redis_excepcion_de_red(self, mock_get):
        self.assertFalse(verificar_redis_via_alb("https://alb.internal"))

    @patch("scripts.post_deploy_smoke.boto3.client")
    def test_verificar_s3_ok(self, mock_boto_client):
        mock_boto_client.return_value.head_bucket.return_value = {}
        self.assertTrue(verificar_s3("mi-bucket", "us-east-1"))
        mock_boto_client.return_value.head_bucket.assert_called_once_with(Bucket="mi-bucket")

    @patch("scripts.post_deploy_smoke.boto3.client")
    def test_verificar_s3_sin_permiso_o_bucket_inexistente(self, mock_boto_client):
        mock_boto_client.return_value.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket"
        )
        self.assertFalse(verificar_s3("mi-bucket", "us-east-1"))

    @patch.dict(
        "os.environ",
        {"SMOKE_ALB_BASE_URL": "https://alb.internal", "AWS_S3_BUCKET": "mi-bucket", "AWS_REGION": "us-east-1"},
        clear=True,
    )
    @patch("scripts.post_deploy_smoke.verificar_s3", return_value=True)
    @patch("scripts.post_deploy_smoke.verificar_redis_via_alb", return_value=True)
    def test_main_exito_cuando_ambos_chequeos_pasan(self, mock_redis, mock_s3):
        self.assertEqual(main(), 0)

    @patch.dict(
        "os.environ",
        {"SMOKE_ALB_BASE_URL": "https://alb.internal", "AWS_S3_BUCKET": "mi-bucket"},
        clear=True,
    )
    @patch("scripts.post_deploy_smoke.verificar_s3", return_value=True)
    @patch("scripts.post_deploy_smoke.verificar_redis_via_alb", return_value=False)
    def test_main_falla_si_redis_falla(self, mock_redis, mock_s3):
        self.assertEqual(main(), 1)

    @patch.dict(
        "os.environ",
        {"SMOKE_ALB_BASE_URL": "https://alb.internal", "AWS_S3_BUCKET": "mi-bucket"},
        clear=True,
    )
    @patch("scripts.post_deploy_smoke.verificar_s3", return_value=False)
    @patch("scripts.post_deploy_smoke.verificar_redis_via_alb", return_value=True)
    def test_main_falla_si_s3_falla(self, mock_redis, mock_s3):
        self.assertEqual(main(), 1)

    @patch.dict("os.environ", {"AWS_S3_BUCKET": "mi-bucket"}, clear=True)
    def test_main_falla_fast_si_falta_smoke_alb_base_url(self):
        self.assertEqual(main(), 1)

    @patch.dict("os.environ", {"SMOKE_ALB_BASE_URL": "https://alb.internal"}, clear=True)
    def test_main_falla_fast_si_falta_aws_s3_bucket(self):
        self.assertEqual(main(), 1)


if __name__ == "__main__":
    unittest.main()
