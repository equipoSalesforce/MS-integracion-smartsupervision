# tests/test_auth_redis_persistence.py
"""
Cobertura de la persistencia compartida en Redis de SfcAuthManager
(_load_from_redis/_save_to_redis/_invalidar_token_compartido_si_coincide) y
el lock distribuido (_acquire_distributed_lock/_release_distributed_lock).
Antes sin cobertura directa -- test_auth_and_signatures.py/
test_auth_concurrency.py prueban el flujo de login/refresh de más alto
nivel, pero no estos helpers de bajo nivel por separado.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.auth import SfcAuthManager
from app.core.security.signatures import SfcSignatureContext


def _crear_manager() -> SfcAuthManager:
    sig_ctx = SfcSignatureContext(secret_key="test-secret")
    return SfcAuthManager(signature_context=sig_ctx, http_client=MagicMock())


class TestLoadFromRedis(unittest.IsolatedAsyncioTestCase):

    async def test_sin_redis_retorna_false(self):
        manager = _crear_manager()
        with patch("app.core.auth.get_redis_client", return_value=None):
            self.assertFalse(await manager._load_from_redis())

    async def test_sin_registro_previo_retorna_false(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(return_value=None)
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            self.assertFalse(await manager._load_from_redis())

    async def test_carga_el_token_compartido_desde_redis(self):
        manager = _crear_manager()
        exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        data = {"access_token": "tok-compartido", "refresh_token": "ref-compartido", "access_exp": exp, "refresh_exp": exp}
        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(data))

        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            resultado = await manager._load_from_redis()

        self.assertTrue(resultado)
        self.assertEqual(manager.access_token, "tok-compartido")
        self.assertEqual(manager.refresh_token, "ref-compartido")

    async def test_error_de_redis_retorna_false_sin_lanzar(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(side_effect=ConnectionError("caido"))
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            self.assertFalse(await manager._load_from_redis())


class TestSaveToRedis(unittest.IsolatedAsyncioTestCase):

    async def test_sin_redis_no_lanza(self):
        manager = _crear_manager()
        with patch("app.core.auth.get_redis_client", return_value=None):
            await manager._save_to_redis()  # No debe lanzar.

    async def test_guarda_el_token_actual_en_redis(self):
        manager = _crear_manager()
        manager.access_token = "tok-nuevo"
        manager.refresh_token = "ref-nuevo"
        manager.access_exp = datetime.now(timezone.utc) + timedelta(hours=1)
        manager.refresh_exp = datetime.now(timezone.utc) + timedelta(days=1)

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock()
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            await manager._save_to_redis()

        mock_redis.set.assert_awaited_once()
        guardado = json.loads(mock_redis.set.call_args[0][1])
        self.assertEqual(guardado["access_token"], "tok-nuevo")

    async def test_error_de_redis_no_propaga(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(side_effect=ConnectionError("caido"))
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            await manager._save_to_redis()  # No debe lanzar.


class TestInvalidarTokenCompartido(unittest.IsolatedAsyncioTestCase):

    async def test_sin_redis_no_hace_nada(self):
        manager = _crear_manager()
        with patch("app.core.auth.get_redis_client", return_value=None):
            await manager._invalidar_token_compartido_si_coincide("tok-rechazado")  # No debe lanzar.

    async def test_sin_token_rechazado_no_llama_a_redis(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock()
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            await manager._invalidar_token_compartido_si_coincide(None)
        mock_redis.eval.assert_not_called()

    async def test_invalida_el_token_compartido_si_coincide(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=1)
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            await manager._invalidar_token_compartido_si_coincide("tok-rechazado")
        mock_redis.eval.assert_awaited_once()

    async def test_error_de_redis_no_propaga(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("caido"))
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            await manager._invalidar_token_compartido_si_coincide("tok-rechazado")  # No debe lanzar.


class TestLockDistribuido(unittest.IsolatedAsyncioTestCase):

    async def test_acquire_sin_redis_retorna_false(self):
        manager = _crear_manager()
        with patch("app.core.auth.get_redis_client", return_value=None):
            self.assertFalse(await manager._acquire_distributed_lock())

    async def test_acquire_exitoso(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=True)
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            self.assertTrue(await manager._acquire_distributed_lock())

    async def test_acquire_fallido_si_ya_esta_tomado(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=None)  # SET NX no aplicó -- la llave ya existía.
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            self.assertFalse(await manager._acquire_distributed_lock())

    async def test_acquire_error_de_redis_retorna_false(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(side_effect=ConnectionError("caido"))
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            self.assertFalse(await manager._acquire_distributed_lock())

    async def test_release_sin_redis_no_lanza(self):
        manager = _crear_manager()
        with patch("app.core.auth.get_redis_client", return_value=None):
            await manager._release_distributed_lock()  # No debe lanzar.

    async def test_release_exitoso(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=1)
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            await manager._release_distributed_lock()
        mock_redis.eval.assert_awaited_once()

    async def test_release_error_de_redis_no_propaga(self):
        manager = _crear_manager()
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("caido"))
        with patch("app.core.auth.get_redis_client", return_value=mock_redis):
            await manager._release_distributed_lock()  # No debe lanzar.


if __name__ == "__main__":
    unittest.main()
