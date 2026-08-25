# tests/test_api_dependencies.py
"""
Cobertura de app/api/dependencies.py -- guards de API Key, y las factories
singleton get_s3_client/get_sfc_client. Antes ~45%: casi toda la suite
reemplaza estas dependencias por mocks inyectados a mano en vez de ejercitar
las factories reales.
"""
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

import httpx
from fastapi import HTTPException

import app.api.dependencies as deps
from app.core.config import settings


class TestVerificarApiKeyAdmin(unittest.IsolatedAsyncioTestCase):

    async def test_key_correcta_retorna_la_key(self):
        resultado = await deps.verificar_api_key_admin(x_api_key=settings.ADMIN_API_KEY)
        self.assertEqual(resultado, settings.ADMIN_API_KEY)

    async def test_key_incorrecta_401(self):
        with self.assertRaises(HTTPException) as ctx:
            await deps.verificar_api_key_admin(x_api_key="clave-invalida")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_key_vacia_401(self):
        with self.assertRaises(HTTPException) as ctx:
            await deps.verificar_api_key_admin(x_api_key="")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_key_no_ascii_da_401_no_500(self):
        """
        🔴 FIX (hallazgo de revisión externa, 2026-08-25): secrets.compare_digest
        lanza TypeError con caracteres no-ASCII -- sin el chequeo .isascii() previo,
        esto se propagaba sin capturar (500 + log crítico) en vez del 401 correcto
        para una credencial inválida.
        """
        with self.assertRaises(HTTPException) as ctx:
            await deps.verificar_api_key_admin(x_api_key="clave-con-ñ-inválida")
        self.assertEqual(ctx.exception.status_code, 401)


class TestVerificarApiKeyCrm(unittest.IsolatedAsyncioTestCase):

    async def test_key_correcta_retorna_la_key(self):
        resultado = await deps.verificar_api_key_crm(x_api_key=settings.CRM_API_KEY)
        self.assertEqual(resultado, settings.CRM_API_KEY)

    async def test_key_incorrecta_401(self):
        with self.assertRaises(HTTPException) as ctx:
            await deps.verificar_api_key_crm(x_api_key="clave-invalida")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_key_no_ascii_da_401_no_500(self):
        with self.assertRaises(HTTPException) as ctx:
            await deps.verificar_api_key_crm(x_api_key="clave-con-ñ-inválida")
        self.assertEqual(ctx.exception.status_code, 401)


class TestGetS3Client(unittest.TestCase):

    def setUp(self):
        self._original = deps._s3_client_instance
        deps._s3_client_instance = None

    def tearDown(self):
        deps._s3_client_instance = self._original

    def test_reutiliza_instancia_ya_creada_sin_llamar_a_boto3(self):
        centinela = object()
        deps._s3_client_instance = centinela
        with patch("app.api.dependencies.boto3.client") as mock_boto:
            resultado = deps.get_s3_client()
        self.assertIs(resultado, centinela)
        mock_boto.assert_not_called()

    def test_con_credenciales_explicitas_pasa_access_key_y_secret(self):
        with patch.object(settings, "AWS_ACCESS_KEY_ID", "AKIA_TEST"), \
             patch.object(settings, "AWS_SECRET_ACCESS_KEY", "secret_test"), \
             patch.object(settings, "AWS_SESSION_TOKEN", None), \
             patch("app.api.dependencies.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            deps.get_s3_client()

        _, kwargs = mock_boto.call_args
        self.assertEqual(kwargs["aws_access_key_id"], "AKIA_TEST")
        self.assertEqual(kwargs["aws_secret_access_key"], "secret_test")
        self.assertNotIn("aws_session_token", kwargs)

    def test_con_session_token_lo_incluye(self):
        with patch.object(settings, "AWS_ACCESS_KEY_ID", "ASIA_TEST"), \
             patch.object(settings, "AWS_SECRET_ACCESS_KEY", "secret_test"), \
             patch.object(settings, "AWS_SESSION_TOKEN", "session-token-temporal"), \
             patch("app.api.dependencies.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            deps.get_s3_client()

        _, kwargs = mock_boto.call_args
        self.assertEqual(kwargs["aws_session_token"], "session-token-temporal")

    def test_sin_credenciales_explicitas_usa_iam_role(self):
        with patch.object(settings, "AWS_ACCESS_KEY_ID", None), \
             patch.object(settings, "AWS_SECRET_ACCESS_KEY", None), \
             patch("app.api.dependencies.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            deps.get_s3_client()

        _, kwargs = mock_boto.call_args
        self.assertNotIn("aws_access_key_id", kwargs)

    def test_fallo_al_inicializar_boto3_retorna_none_sin_lanzar(self):
        with patch.object(settings, "AWS_ACCESS_KEY_ID", "AKIA_TEST"), \
             patch.object(settings, "AWS_SECRET_ACCESS_KEY", "secret_test"), \
             patch("app.api.dependencies.boto3.client", side_effect=RuntimeError("credenciales inválidas")):
            resultado = deps.get_s3_client()

        self.assertIsNone(resultado)


class TestGetSfcClientYHttpClient(unittest.TestCase):

    def test_get_sfc_client_sin_request_usa_http_client_none(self):
        cliente = deps.get_sfc_client(request=None)
        self.assertIs(cliente.interceptor, deps._auth_manager_instance)

    def test_get_sfc_client_reutiliza_http_client_del_app_state(self):
        http_client_app = httpx.AsyncClient()
        request_fake = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=http_client_app)))

        cliente = deps.get_sfc_client(request=request_fake)

        self.assertIs(cliente.client, http_client_app)

    def test_get_sfc_client_con_http_client_explicito(self):
        http_client = httpx.AsyncClient()
        cliente = deps.get_sfc_client_con_http_client(http_client)
        self.assertIs(cliente.client, http_client)
        self.assertIs(cliente.interceptor, deps._auth_manager_instance)


if __name__ == "__main__":
    unittest.main()
