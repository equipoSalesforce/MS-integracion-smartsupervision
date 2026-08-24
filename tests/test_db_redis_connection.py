# tests/test_db_redis_connection.py
"""
Cobertura de app/db/redis.py: init_redis/close_redis/get_redis_client/ping_redis
y el loop de auto-reconexión en segundo plano. Antes 41% -- casi toda la suite
inyecta un cliente Redis ya construido (real o mock) en vez de ejercitar esta
orquestación de conexión/reconexión.

Manipula directamente los globales del módulo (redis_client, _reconnect_task);
cada test los restaura en tearDown para no filtrar estado a otros tests de la
suite que sí dependen de un Redis real inyectado aparte.
"""
import asyncio
import unittest
from unittest.mock import patch, AsyncMock

import app.db.redis as redis_module


class _RedisModuleStateTestCase(unittest.IsolatedAsyncioTestCase):
    """Guarda/restaura los globales mutables de app.db.redis alrededor de cada test."""

    async def asyncSetUp(self):
        self._orig_client = redis_module.redis_client
        self._orig_task = redis_module._reconnect_task
        redis_module.redis_client = None
        redis_module._reconnect_task = None

    async def asyncTearDown(self):
        if redis_module._reconnect_task and not redis_module._reconnect_task.done():
            redis_module._reconnect_task.cancel()
            try:
                await redis_module._reconnect_task
            except asyncio.CancelledError:
                pass
        redis_module.redis_client = self._orig_client
        redis_module._reconnect_task = self._orig_task


class TestInitRedis(_RedisModuleStateTestCase):

    async def test_conexion_exitosa_deja_redis_client_asignado(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)

        with patch("app.db.redis._build_redis_client", return_value=mock_client):
            resultado = await redis_module.init_redis()

        self.assertTrue(resultado)
        self.assertIs(redis_module.redis_client, mock_client)

    async def test_conexion_fallida_cierra_cliente_y_arranca_reconexion(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=ConnectionError("Redis caído"))
        mock_client.aclose = AsyncMock()

        with patch("app.db.redis._build_redis_client", return_value=mock_client), \
             patch("app.db.redis._iniciar_tarea_reconexion") as mock_iniciar_reconexion:
            resultado = await redis_module.init_redis()

        self.assertFalse(resultado)
        self.assertIsNone(redis_module.redis_client)
        mock_client.aclose.assert_awaited_once()
        mock_iniciar_reconexion.assert_called_once()

    async def test_si_ya_hay_cliente_no_reconstruye(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        redis_module.redis_client = mock_client

        with patch("app.db.redis._build_redis_client") as mock_build:
            resultado = await redis_module.init_redis()

        self.assertTrue(resultado)
        mock_build.assert_not_called()
        mock_client.ping.assert_awaited_once()


class TestCloseRedis(_RedisModuleStateTestCase):

    async def test_cierra_cliente_y_lo_deja_en_none(self):
        mock_client = AsyncMock()
        redis_module.redis_client = mock_client

        await redis_module.close_redis()

        mock_client.aclose.assert_awaited_once()
        self.assertIsNone(redis_module.redis_client)

    async def test_error_al_cerrar_no_propaga_y_igual_limpia(self):
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock(side_effect=RuntimeError("socket ya cerrado"))
        redis_module.redis_client = mock_client

        await redis_module.close_redis()  # No debe lanzar.

        self.assertIsNone(redis_module.redis_client)

    async def test_cancela_tarea_de_reconexion_pendiente(self):
        tarea = asyncio.create_task(asyncio.sleep(100))
        redis_module._reconnect_task = tarea

        await redis_module.close_redis()

        self.assertTrue(tarea.cancelled() or tarea.done())
        self.assertIsNone(redis_module._reconnect_task)

    async def test_sin_cliente_no_hace_nada(self):
        redis_module.redis_client = None
        await redis_module.close_redis()  # No debe lanzar.
        self.assertIsNone(redis_module.redis_client)


class TestReintentarConexionBackground(_RedisModuleStateTestCase):

    async def test_reconecta_al_primer_intento_exitoso(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)

        with patch("app.db.redis._build_redis_client", return_value=mock_client), \
             patch("app.db.redis.asyncio.sleep", new_callable=AsyncMock):
            await redis_module._reintentar_conexion_background()

        self.assertIs(redis_module.redis_client, mock_client)

    async def test_cierra_candidato_fallido_antes_de_reintentar(self):
        mock_client_falla = AsyncMock()
        mock_client_falla.ping = AsyncMock(side_effect=ConnectionError("aún caído"))
        mock_client_falla.aclose = AsyncMock()

        mock_client_exito = AsyncMock()
        mock_client_exito.ping = AsyncMock(return_value=True)

        with patch("app.db.redis._build_redis_client", side_effect=[mock_client_falla, mock_client_exito]), \
             patch("app.db.redis.asyncio.sleep", new_callable=AsyncMock):
            await redis_module._reintentar_conexion_background()

        mock_client_falla.aclose.assert_awaited_once()
        self.assertIs(redis_module.redis_client, mock_client_exito)


class TestIniciarTareaReconexion(unittest.TestCase):
    """Sync a propósito: sin loop corriendo, ejercita la rama RuntimeError."""

    def setUp(self):
        self._orig_task = redis_module._reconnect_task
        redis_module._reconnect_task = None

    def tearDown(self):
        redis_module._reconnect_task = self._orig_task

    def test_sin_loop_activo_no_lanza(self):
        redis_module._iniciar_tarea_reconexion()  # No debe lanzar aunque no haya loop.
        self.assertIsNone(redis_module._reconnect_task)


class TestGetRedisClient(_RedisModuleStateTestCase):

    async def test_retorna_cliente_existente_sin_disparar_reconexion(self):
        mock_client = AsyncMock()
        redis_module.redis_client = mock_client

        with patch("app.db.redis._iniciar_tarea_reconexion") as mock_iniciar:
            resultado = redis_module.get_redis_client()

        self.assertIs(resultado, mock_client)
        mock_iniciar.assert_not_called()

    async def test_none_dispara_intento_de_reconexion(self):
        redis_module.redis_client = None

        with patch("app.db.redis._iniciar_tarea_reconexion") as mock_iniciar:
            resultado = redis_module.get_redis_client()

        self.assertIsNone(resultado)
        mock_iniciar.assert_called_once()


class TestPingRedis(_RedisModuleStateTestCase):

    async def test_sin_cliente_retorna_false(self):
        redis_module.redis_client = None
        self.assertFalse(await redis_module.ping_redis())

    async def test_ping_exitoso_retorna_true(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        redis_module.redis_client = mock_client
        self.assertTrue(await redis_module.ping_redis())

    async def test_ping_fallido_retorna_false_sin_lanzar(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=ConnectionError("timeout"))
        redis_module.redis_client = mock_client
        self.assertFalse(await redis_module.ping_redis())


if __name__ == "__main__":
    unittest.main()
