# tests/test_auth_and_signatures.py
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import datetime
import jwt

from app.core.config import settings
from app.core.auth import SfcAuthManager
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


if __name__ == "__main__":
    unittest.main()