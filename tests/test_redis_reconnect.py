# tests/test_redis_reconnect.py
"""
Auditoría 2026-08-13, item 14 / P1-01: durante una caída prolongada de Redis,
cada intento fallido de reconexión en background debía cerrar explícitamente
el cliente candidato — si no, el pool de conexiones de cada intento fallido
queda abierto sin referenciar, acumulándose indefinidamente durante el outage.
"""
import unittest
from unittest.mock import AsyncMock, patch

import app.db.redis as redis_module


class TestRedisReconnectCleanup(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._redis_client_original = redis_module.redis_client
        redis_module.redis_client = None

    async def asyncTearDown(self):
        redis_module.redis_client = self._redis_client_original

    async def test_candidato_fallido_se_cierra_antes_de_reintentar(self):
        cliente_fallido = AsyncMock()
        cliente_fallido.ping = AsyncMock(side_effect=ConnectionError("Redis caído"))

        cliente_exitoso = AsyncMock()
        cliente_exitoso.ping = AsyncMock(return_value=True)

        with patch("app.db.redis.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.db.redis._build_redis_client", side_effect=[cliente_fallido, cliente_exitoso]):

            await redis_module._reintentar_conexion_background()

        cliente_fallido.aclose.assert_called_once()
        cliente_exitoso.aclose.assert_not_called()
        self.assertIs(redis_module.redis_client, cliente_exitoso)

    async def test_varios_intentos_fallidos_cierran_cada_candidato(self):
        clientes_fallidos = [AsyncMock() for _ in range(3)]
        for c in clientes_fallidos:
            c.ping = AsyncMock(side_effect=ConnectionError("Redis caído"))

        cliente_exitoso = AsyncMock()
        cliente_exitoso.ping = AsyncMock(return_value=True)

        with patch("app.db.redis.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.db.redis._build_redis_client", side_effect=[*clientes_fallidos, cliente_exitoso]):

            await redis_module._reintentar_conexion_background()

        for c in clientes_fallidos:
            c.aclose.assert_called_once()
        self.assertIs(redis_module.redis_client, cliente_exitoso)

    async def test_fallo_al_cerrar_candidato_no_rompe_el_loop_de_reintento(self):
        """Un aclose() que también falle no debe impedir que el loop siga reintentando."""
        cliente_fallido = AsyncMock()
        cliente_fallido.ping = AsyncMock(side_effect=ConnectionError("Redis caído"))
        cliente_fallido.aclose = AsyncMock(side_effect=RuntimeError("aclose también falló"))

        cliente_exitoso = AsyncMock()
        cliente_exitoso.ping = AsyncMock(return_value=True)

        with patch("app.db.redis.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.db.redis._build_redis_client", side_effect=[cliente_fallido, cliente_exitoso]):

            await redis_module._reintentar_conexion_background()

        self.assertIs(redis_module.redis_client, cliente_exitoso)


if __name__ == "__main__":
    unittest.main()
