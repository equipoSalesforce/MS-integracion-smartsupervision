# tests/test_auth_and_signatures.py
import unittest
from unittest.mock import MagicMock, patch
import datetime
import httpx
import jwt

from app.core.auth import SfcAuthManager
from app.core.exceptions import SfcIntegrationException
from app.core.security.signatures import (
    SfcSignatureContext,
    UrlSignatureStrategy,
    PayloadSignatureStrategy,
    FileTransferSignatureStrategy
)

def generate_mock_jwt(expires_in_minutes: int) -> str:
    """Helper para generar tokens JWT válidos para simular la SFC."""
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "exp": int((now + datetime.timedelta(minutes=expires_in_minutes)).timestamp()),
        "token_type": "access",
        "user_id": 1
    }
    secure_test_key = "secure_test_key_at_least_32_bytes_long_for_jws"
    return jwt.encode(payload, secure_test_key, algorithm="HS256")


class TestAuthAndSignatures(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.secret_key = "G66_SFC_TEST_SECRET_KEY"
        self.signature_context = SfcSignatureContext(self.secret_key)
        
        self.mock_access_token = generate_mock_jwt(30)
        self.mock_refresh_token = generate_mock_jwt(720)

    # ==========================================
    # 1. PRUEBAS DE FIRMAS (PATRÓN STRATEGY)
    # ==========================================

    def test_url_signature_strategy(self):
        """Verifica que la firma de URLs (GET) se genere correctamente."""
        url = "https://qasmart.superfinanciera.gov.co/api/queja/"
        strategy = UrlSignatureStrategy(self.secret_key)
        
        signature = strategy.sign(url)
        
        self.assertIsNotNone(signature)
        self.assertTrue(signature.isupper())
        self.assertEqual(len(signature), 64)

    def test_payload_signature_strategy(self):
        """Verifica que la firma de JSONs (POST/PUT/PATCH) use ensure_ascii=False."""
        payload = {"nombres": "Ramón Peña", "asunto": "Cierre de queja"}
        strategy = PayloadSignatureStrategy(self.secret_key)
        
        signature = strategy.sign(payload)
        
        self.assertIsNotNone(signature)
        self.assertEqual(len(signature), 64)

    def test_file_transfer_signature_strategy(self):
        """Verifica que la firma de archivos ignore el binario y use solo metadatos."""
        payload_with_file = {
            "codigo_queja": "123456789",
            "type": "pdf",
            "file": "este-es-un-binario-pesado-que-debe-ser-ignorado"
        }
        
        strategy = FileTransferSignatureStrategy(self.secret_key)
        signature_with_file = strategy.sign(payload_with_file)
        
        strategy_base = PayloadSignatureStrategy(self.secret_key)
        signature_base = strategy_base.sign({"codigo_queja": "123456789", "type": "pdf"})
        
        self.assertEqual(signature_with_file, signature_base)

    # ==========================================
    # 2. PRUEBAS DEL GESTOR DE AUTENTICACIÓN
    # ==========================================

    @patch("app.core.auth.httpx.AsyncClient.post")
    async def test_auth_manager_login_exitoso(self, mock_post):
        """Prueba que el gestor obtenga y guarde los tokens tras un login exitoso."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access": self.mock_access_token,
            "refresh": self.mock_refresh_token
        }
        mock_post.return_value = mock_response

        auth_manager = SfcAuthManager(self.signature_context)
        token = await auth_manager.get_valid_token()

        self.assertEqual(token, self.mock_access_token)
        self.assertEqual(auth_manager.refresh_token, self.mock_refresh_token)
        self.assertIsNotNone(auth_manager.access_exp)
        mock_post.assert_called_once()

    @patch("app.core.auth.httpx.AsyncClient.post")
    async def test_auth_manager_automatic_refresh(self, mock_post):
        """Prueba que si el access token expira pero el refresh sigue vivo, se use /api/token/refresh."""
        auth_manager = SfcAuthManager(self.signature_context)
        
        expired_access = generate_mock_jwt(-5)
        auth_manager.access_token = expired_access
        auth_manager.refresh_token = self.mock_refresh_token
        auth_manager.access_exp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        auth_manager.refresh_exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=11)

        new_access = generate_mock_jwt(30)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access": new_access,
            "refresh": self.mock_refresh_token
        }
        mock_post.return_value = mock_response

        token = await auth_manager.get_valid_token()

        self.assertEqual(token, new_access)
        self.assertIn("/api/token/refresh", str(mock_post.call_args[0][0]))

    @patch("app.core.auth.httpx.AsyncClient.post")
    async def test_login_con_body_no_json_se_clasifica_como_falla_transitoria(self, mock_post):
        """
        🔴 FIX (hallazgo propio, 2026-08-27): si la SFC respondiera con un cuerpo que
        no es JSON válido (ej. una página de error de un proxy/LB mal configurado
        durante una caída real) el fallo de _login no tenía ningún try/except propio
        -- la excepción cruda (json.JSONDecodeError) no coincide con ningún `except`
        de sfc_client.py/routes_quejas.py y nunca se encolaba para reintento
        automático, a diferencia de una caída de conectividad real contra la SFC.
        Debe envolverse en SfcIntegrationException, transitoria.
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
        mock_post.return_value = mock_response

        auth_manager = SfcAuthManager(self.signature_context)

        with self.assertRaises(SfcIntegrationException) as ctx:
            await auth_manager.get_valid_token()

        self.assertEqual(ctx.exception.error_type, "SFC_AUTH_RESPONSE_INVALID")
        self.assertTrue(ctx.exception.es_transitoria)

    @patch("app.core.auth.httpx.AsyncClient.post")
    async def test_login_con_token_mal_formado_se_clasifica_como_falla_transitoria(self, mock_post):
        """Mismo hallazgo, pero con un JSON válido cuyo 'access' no es un JWT
        decodificable (jwt.decode lanza dentro de _save_tokens_local)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access": "esto-no-es-un-jwt-valido",
            "refresh": self.mock_refresh_token
        }
        mock_post.return_value = mock_response

        auth_manager = SfcAuthManager(self.signature_context)

        with self.assertRaises(SfcIntegrationException) as ctx:
            await auth_manager.get_valid_token()

        self.assertEqual(ctx.exception.error_type, "SFC_AUTH_RESPONSE_INVALID")
        self.assertTrue(ctx.exception.es_transitoria)

    @patch("app.core.auth.httpx.AsyncClient.post")
    async def test_login_con_4xx_del_propio_endpoint_se_clasifica_como_falla_transitoria(self, mock_post):
        """
        🔴 FIX (hallazgo propio, 2026-08-27): un 4xx/5xx del propio endpoint
        /api/login/ (credenciales rotadas, hiccup del servicio de auth de la SFC) no
        tenía NINGÚN try/except sobre `raise_for_status()` -- el httpx.HTTPStatusError
        crudo escapaba de auth_flow (se dispara ANTES del `yield request`) hasta el
        try/except de sfc_client.py que envuelve la llamada de NEGOCIO real, donde se
        clasificaba con SfcErrorTranslator contra el texto de la respuesta de LOGIN
        como si fuera un rechazo de negocio de la queja -- casi nunca matchea nada del
        catálogo y cae a UNKNOWN_SFC_ERROR (es_transitoria=False), descartando
        permanentemente una operación que en realidad falló por un problema
        transitorio de autenticación. Debe envolverse en SfcIntegrationException,
        transitoria, igual que el resto de fallas de respuesta de auth.
        """
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Forbidden", request=MagicMock(), response=mock_response
        )
        mock_post.return_value = mock_response

        auth_manager = SfcAuthManager(self.signature_context)

        with self.assertRaises(SfcIntegrationException) as ctx:
            await auth_manager.get_valid_token()

        self.assertEqual(ctx.exception.error_type, "SFC_AUTH_RESPONSE_INVALID")
        self.assertTrue(ctx.exception.es_transitoria)

    @patch("app.core.auth.httpx.AsyncClient.post")
    async def test_refresh_con_5xx_no_401_se_clasifica_como_falla_transitoria(self, mock_post):
        """Mismo hallazgo que el login, pero en /api/token/refresh -- el 401 explícito
        ya se maneja aparte (deliberado, cae a login completo), pero cualquier OTRO
        4xx/5xx (ej. 500 del servicio de auth) no estaba envuelto."""
        auth_manager = SfcAuthManager(self.signature_context)

        expired_access = generate_mock_jwt(-5)
        auth_manager.access_token = expired_access
        auth_manager.refresh_token = self.mock_refresh_token
        auth_manager.access_exp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        auth_manager.refresh_exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=11)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Internal Server Error", request=MagicMock(), response=mock_response
        )
        mock_post.return_value = mock_response

        with self.assertRaises(SfcIntegrationException) as ctx:
            await auth_manager.get_valid_token()

        self.assertEqual(ctx.exception.error_type, "SFC_AUTH_RESPONSE_INVALID")
        self.assertTrue(ctx.exception.es_transitoria)


if __name__ == "__main__":
    unittest.main()