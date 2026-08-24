# tests/test_auth_resource_leak.py
import unittest
from unittest.mock import AsyncMock, patch
import httpx

from app.core.auth import SfcAuthManager
from app.core.security.signatures import SfcSignatureContext
from app.main import app, lifespan

class TestAuthResourceLeak(unittest.IsolatedAsyncioTestCase):

    async def test_auth_manager_close_closes_internal_client(self):
        """Verifica que close() libere las conexiones y cierre el cliente HTTPX interno."""
        sig_ctx = SfcSignatureContext("secret_key_testing_2026_1234567890")
        auth_manager = SfcAuthManager(sig_ctx)

        self.assertFalse(auth_manager.client.is_closed)
        await auth_manager.close()
        self.assertTrue(auth_manager.client.is_closed)

    async def test_auth_manager_does_not_close_injected_external_client(self):
        """Verifica que si el cliente HTTP fue inyectado, SfcAuthManager no intente adueñarse de su cierre."""
        sig_ctx = SfcSignatureContext("secret_key_testing_2026_1234567890")
        external_client = httpx.AsyncClient()
        
        auth_manager = SfcAuthManager(sig_ctx, http_client=external_client)
        await auth_manager.close()

        # El cliente externo debe permanecer abierto para el resto de la app
        self.assertFalse(external_client.is_closed)
        await external_client.aclose()

    @patch("app.main._auth_manager_instance.close", new_callable=AsyncMock)
    @patch("app.main.init_redis", new_callable=AsyncMock)
    @patch("app.main.close_redis", new_callable=AsyncMock)
    async def test_lifespan_shutdown_triggers_auth_manager_close(
        self, mock_close_redis, mock_init_redis, mock_auth_close
    ):
        """Verifica que el evento de apagado (shutdown) en main.py invoque close() sobre el singleton de autenticación."""
        async with lifespan(app):
            pass  # Ejecuta startup y shutdown dentro del bloque de contexto

        mock_auth_close.assert_called_once()