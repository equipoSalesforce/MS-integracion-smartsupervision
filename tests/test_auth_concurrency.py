import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone

from app.core.auth import SfcAuthManager
from app.core.security.signatures import SfcSignatureContext

class TestAuthConcurrency(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.signature_ctx = SfcSignatureContext("secret_key_test_12345678901234567890")
        self.auth_manager = SfcAuthManager(self.signature_ctx)

    async def asyncTearDown(self):
        await self.auth_manager.close()

    async def test_thundering_herd_prevention_on_token_refresh(self):
        """
        Verifica que ante 50 peticiones concurrentes con access_token expirado,
        el Double-Checked Locking dispare ÚNICAMENTE 1 llamada HTTP de refresco a la SFC.
        """
        now = datetime.now(timezone.utc)
        
        # 1. Configurar estado inicial: Access Token expirado, Refresh Token válido
        self.auth_manager.access_token = "OLD_EXPIRED_TOKEN"
        self.auth_manager.access_exp = now - timedelta(minutes=10) # Expirado
        self.auth_manager.refresh_token = "VALID_REFRESH_TOKEN"
        self.auth_manager.refresh_exp = now + timedelta(hours=10) # Válido

        # 2. Mockear la respuesta HTTP de la SFC simulando una latencia de red de 100ms
        async def mock_post_refresh(endpoint, json, headers):
            await asyncio.sleep(0.1)  # Simula tiempo de respuesta de red
            mock_res = MagicMock()
            mock_res.status_code = 200
            mock_res.json.return_value = {
                # JWTs ficticios con exp en el año 2050
                "access": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjI1MjQ2MDgwMDB9.mock_access",
                "refresh": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjI1MjQ2MDgwMDB9.mock_refresh"
            }
            return mock_res

        self.auth_manager.client.post = AsyncMock(side_effect=mock_post_refresh)

        # 3. Disparar 50 peticiones simultáneas con asyncio.gather
        num_concurrencia = 50
        tasks = [self.auth_manager.get_valid_token() for _ in range(num_concurrencia)]
        tokens_obtenidos = await asyncio.gather(*tasks)

        # 4. Aserciones de verificación
        # A) Todas las 50 corrutinas deben haber recibido EXACTAMENTE el mismo token
        tokens_unicos = set(tokens_obtenidos)
        self.assertEqual(
            len(tokens_unicos), 1, 
            f"Se esperaban 1 token único, pero se obtuvieron {len(tokens_unicos)}"
        )

        # B) La llamada HTTP POST a /api/token/refresh debe haberse realizado EXACTAMENTE 1 vez
        self.assertEqual(
            self.auth_manager.client.post.call_count, 1,
            f"El candado falló: se esperaban 1 llamada a la SFC, pero se realizaron {self.auth_manager.client.post.call_count}"
        )