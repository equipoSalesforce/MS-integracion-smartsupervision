# tests/test_auth_concurrency_real_redis.py
"""
Auditoría de concurrencia (2026-08-26): SfcAuthManager (app/core/auth.py) coordina
la renovación de tokens entre múltiples tasks ECS vía un lock distribuido en Redis
y dos scripts Lua propios (RELEASE_LOCK_LUA_SCRIPT, INVALIDATE_IF_MATCHES_LUA_SCRIPT),
separados de RedisLock. Ninguno de los dos se había ejecutado nunca contra Redis real
-- test_auth_redis_persistence.py mockea el cliente por completo.

Más importante: test_auth_concurrency.py::test_thundering_herd_prevention_on_token_refresh
prueba 50 llamadas concurrentes, pero TODAS sobre la MISMA instancia de SfcAuthManager
-- el `_local_lock` (asyncio.Lock, propio del proceso) ya serializa esas 50 corrutinas
antes de que la mayoría llegue siquiera a tocar el lock distribuido. Eso nunca prueba
la coordinación real entre INSTANCIAS SEPARADAS (el escenario real: 2+ tasks ECS, cada
una con su propio proceso y su propio `_local_lock`), que es exactamente para lo que
existe el lock de Redis. Este archivo cubre ambas brechas.
"""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.core.auth import SfcAuthManager, REDIS_TOKEN_KEY, REDIS_LOCK_KEY
from app.core.security.signatures import SfcSignatureContext

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


def _redis_disponible() -> bool:
    if redis_asyncio is None:
        return False

    async def _check():
        client = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        try:
            await client.ping()
            return True
        except Exception:
            return False
        finally:
            await client.aclose()

    try:
        return asyncio.run(_check())
    except Exception:
        return False


_REDIS_OK = _redis_disponible()


def _generar_jwt(minutos: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {"exp": int((now + timedelta(minutes=minutos)).timestamp()), "user_id": 1}
    return jwt.encode(payload, "secret_key_testing_32_bytes_long!!", algorithm="HS256")


def _crear_manager() -> SfcAuthManager:
    sig_ctx = SfcSignatureContext("secret_key_test_12345678901234567890")
    return SfcAuthManager(signature_context=sig_ctx, http_client=MagicMock())


@unittest.skipUnless(
    _REDIS_OK,
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de SfcAuthManager "
    "contra Redis real. Levante un Redis local (ej. `docker run --rm -p 6379:6379 "
    "redis:7-alpine`) para ejecutarlas."
)
class TestAuthLuaScriptsContraRedisReal(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.manager = _crear_manager()
        self._patcher = patch("app.core.auth.get_redis_client", return_value=self.redis)
        self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()
        await self.manager.close()
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_acquire_y_release_distributed_lock_round_trip(self):
        self.assertTrue(await self.manager._acquire_distributed_lock())
        self.assertEqual(await self.redis.get(REDIS_LOCK_KEY), self.manager._owner_id)

        await self.manager._release_distributed_lock()
        self.assertIsNone(await self.redis.get(REDIS_LOCK_KEY))

    async def test_release_no_borra_lock_de_otro_dueno(self):
        """Cubre el mismo script CAD que RedisLock, pero en la implementación
        separada y propia de auth.py -- verifica que no divergió en un bug."""
        await self.redis.set(REDIS_LOCK_KEY, "otra-instancia-ecs", px=15000)

        await self.manager._release_distributed_lock()

        self.assertEqual(await self.redis.get(REDIS_LOCK_KEY), "otra-instancia-ecs")

    async def test_invalida_el_token_compartido_si_coincide(self):
        exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        await self.redis.set(REDIS_TOKEN_KEY, json.dumps({
            "access_token": "tok-rechazado", "refresh_token": "ref", "access_exp": exp, "refresh_exp": exp
        }))

        await self.manager._invalidar_token_compartido_si_coincide("tok-rechazado")

        self.assertIsNone(await self.redis.get(REDIS_TOKEN_KEY))

    async def test_no_invalida_el_token_compartido_si_no_coincide(self):
        """El caso más importante del script: otra instancia YA renovó el token (es
        distinto del que este proceso intentó usar) -- invalidar a ciegas borraría un
        token fresco que otra instancia acaba de guardar."""
        exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        await self.redis.set(REDIS_TOKEN_KEY, json.dumps({
            "access_token": "tok-ya-renovado-por-otra-instancia", "refresh_token": "ref",
            "access_exp": exp, "refresh_exp": exp
        }))

        await self.manager._invalidar_token_compartido_si_coincide("tok-rechazado-viejo")

        raw = await self.redis.get(REDIS_TOKEN_KEY)
        self.assertIsNotNone(raw)
        self.assertEqual(json.loads(raw)["access_token"], "tok-ya-renovado-por-otra-instancia")


@unittest.skipUnless(
    _REDIS_OK,
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de coordinación "
    "multi-instancia de SfcAuthManager."
)
class TestAuthCoordinacionEntreInstanciasSeparadas(unittest.IsolatedAsyncioTestCase):
    """
    A diferencia de test_auth_concurrency.py (una sola instancia, lock local), aquí
    cada SfcAuthManager es una instancia INDEPENDIENTE con su propio `_local_lock` --
    la única coordinación posible entre ellas es el lock distribuido de Redis. Esto
    simula el escenario real: N tasks ECS, cada una un proceso separado.
    """

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self._patcher = patch("app.core.auth.get_redis_client", return_value=self.redis)
        self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_solo_una_instancia_hace_login_las_demas_reusan_el_token_de_redis(self):
        N = 8
        managers = [_crear_manager() for _ in range(N)]
        nuevo_access = _generar_jwt(30)
        nuevo_refresh = _generar_jwt(720)

        async def mock_login(self_ref, *_a, **_k):
            # Simula latencia real de red -- ensancha la ventana de la carrera para
            # que las demás instancias realmente compitan mientras esta sigue en vuelo.
            await asyncio.sleep(0.05)
            self_ref._save_tokens_local(nuevo_access, nuevo_refresh)

        for m in managers:
            m._login = mock_login.__get__(m, SfcAuthManager)

        try:
            tokens = await asyncio.gather(*[m.get_valid_token() for m in managers])

            self.assertEqual(len(set(tokens)), 1, "Todas las instancias deben terminar con el MISMO token")
            self.assertEqual(tokens[0], nuevo_access)

            raw = await self.redis.get(REDIS_TOKEN_KEY)
            self.assertIsNotNone(raw, "El token debe haber quedado persistido en Redis para instancias futuras")
            self.assertEqual(json.loads(raw)["access_token"], nuevo_access)
        finally:
            await asyncio.gather(*[m.close() for m in managers])

    async def test_instancia_perdedora_de_la_carrera_del_lock_espera_y_reusa_sin_llamar_a_la_sfc(self):
        """
        Reproduce el camino de espera de _renovar_token_coordinado: una instancia
        mantiene el lock ocupado (simulando que está en medio de un login real); una
        segunda instancia, con el mismo estado (access expirado, sin refresh válido),
        debe hacer polling, encontrar el lock tomado, y terminar reutilizando el token
        que la primera dejó en Redis -- sin hacer su propia llamada HTTP.
        """
        ganador = _crear_manager()
        perdedor = _crear_manager()
        nuevo_access = _generar_jwt(30)
        nuevo_refresh = _generar_jwt(720)

        # El "ganador" ya sostiene el lock distribuido manualmente (simula estar en
        # medio de su propio login) y lo libera tras dejar el token en Redis.
        self.assertTrue(await ganador._acquire_distributed_lock())

        async def liberar_tras_login_simulado():
            await asyncio.sleep(0.15)
            ganador._save_tokens_local(nuevo_access, nuevo_refresh)
            await ganador._save_to_redis()
            await ganador._release_distributed_lock()

        perdedor._login = AsyncMock(side_effect=AssertionError("El perdedor NO debe llamar a login()"))
        perdedor._refresh_access_token = AsyncMock(
            side_effect=AssertionError("El perdedor NO debe llamar a refresh()")
        )

        try:
            _, token_perdedor = await asyncio.gather(
                liberar_tras_login_simulado(),
                perdedor.get_valid_token()
            )

            self.assertEqual(token_perdedor, nuevo_access)
            perdedor._login.assert_not_called()
            perdedor._refresh_access_token.assert_not_called()
        finally:
            await asyncio.gather(ganador.close(), perdedor.close())


if __name__ == "__main__":
    unittest.main()
