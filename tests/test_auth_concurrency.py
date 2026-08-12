# tests/test_auth_concurrency.py
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

import jwt
from app.core.auth import SfcAuthManager
from app.core.security.signatures import SfcSignatureContext


def generate_mock_jwt(expires_in_minutes: int) -> str:
    """Helper para generar tokens JWT válidos simulados."""
    now = datetime.now(timezone.utc)
    payload = {
        "exp": int((now + timedelta(minutes=expires_in_minutes)).timestamp()),
        "user_id": 1
    }
    return jwt.encode(payload, "secret_key_testing_32_bytes_long!!", algorithm="HS256")


class MockRedisAuthStore:
    """Emulador de Redis para pruebas de coordinación distribuida de autenticación."""

    def __init__(self):
        self.store = {}
        self.lock_owner = None

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, nx=False, px=None, ex=None):
        if nx and key in self.store:
            return None  # No se adquirió el candado distribuido
        self.store[key] = str(value)
        return True

    async def eval(self, script, numkeys, *args):
        # Emulación de liberación o invalidación de lock en Lua
        return 1


class TestAuthConcurrency(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.signature_ctx = SfcSignatureContext("secret_key_test_12345678901234567890")
        self.auth_manager = SfcAuthManager(self.signature_ctx)
        self.mock_redis = MockRedisAuthStore()

        self.mock_access_token = generate_mock_jwt(30)
        self.mock_refresh_token = generate_mock_jwt(720)

    async def asyncTearDown(self):
        await self.auth_manager.close()

    @patch("app.core.auth.get_redis_client")
    async def test_thundering_herd_prevention_on_token_refresh(self, mock_get_redis):
        """
        Verifica que ante 50 peticiones concurrentes en cluster con access_token expirado,
        el Lock Distribuido en Redis coordine la operación y dispare ÚNICAMENTE 1 llamada HTTP de refresco a la SFC.
        """
        mock_get_redis.return_value = self.mock_redis
        now = datetime.now(timezone.utc)

        # 1. Estado inicial: Access Token expirado, Refresh Token válido
        expired_access = generate_mock_jwt(-10)
        self.auth_manager.access_token = expired_access
        self.auth_manager.access_exp = now - timedelta(minutes=10)
        self.auth_manager.refresh_token = self.mock_refresh_token
        self.auth_manager.refresh_exp = now + timedelta(hours=10)

        # 2. Mockear llamada HTTP POST con latencia simulada
        async def mock_post_refresh(endpoint, json, headers):
            await asyncio.sleep(0.05)  # Simula latencia de red
            mock_res = MagicMock()
            mock_res.status_code = 200
            mock_res.json.return_value = {
                "access": self.mock_access_token,
                "refresh": self.mock_refresh_token
            }
            return mock_res

        self.auth_manager.client.post = AsyncMock(side_effect=mock_post_refresh)

        # 3. Disparar 50 peticiones simultáneas con asyncio.gather
        num_concurrencia = 50
        tasks = [self.auth_manager.get_valid_token() for _ in range(num_concurrencia)]
        tokens_obtenidos = await asyncio.gather(*tasks)

        # 4. Aserciones
        tokens_unicos = set(tokens_obtenidos)
        self.assertEqual(
            len(tokens_unicos), 1,
            f"Se esperaba 1 token único, pero se obtuvieron {len(tokens_unicos)}"
        )

        # Confirmar que solo se realizó 1 llamada HTTP a la SFC
        self.assertEqual(
            self.auth_manager.client.post.call_count, 1,
            f"Se esperaba 1 llamada a la SFC, pero se realizaron {self.auth_manager.client.post.call_count}"
        )

        # Confirmar que el token renovado quedó guardado en Redis para las demás instancias
        raw_redis_token = await self.mock_redis.get("{sfc:auth}:tokens")
        self.assertIsNotNone(raw_redis_token)
        data_redis = json.loads(raw_redis_token)
        self.assertEqual(data_redis["access_token"], self.mock_access_token)


if __name__ == "__main__":
    unittest.main()