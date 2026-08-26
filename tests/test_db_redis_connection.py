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
from unittest.mock import patch, AsyncMock, MagicMock

import app.db.redis as redis_module
from app.core.config import settings


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


class TestBuildRedisClient(unittest.TestCase):
    """
    Cobertura de _build_redis_client -- antes sin ejercitar (toda la suite mockea
    la función completa en vez de sus ramas internas). Se patchean los constructores
    de redis.asyncio directamente (el import dentro de la función sólo re-vincula
    el nombre local al mismo módulo ya cargado en sys.modules, así que el patch
    sobre el módulo real se ve igual desde adentro).

    Cubre en particular REDIS_CLUSTER_MODE=True -- el modo real usado en producción
    contra AWS ElastiCache Cluster (ver config.py::_validar_redis_produccion), que
    no tenía ninguna cobertura: un kwarg incorrecto ahí sólo se habría descubierto
    en un despliegue real.
    """

    def test_sin_cluster_ni_url_usa_redis_por_host_puerto(self):
        with patch.object(settings, "REDIS_CLUSTER_MODE", False), \
             patch.object(settings, "REDIS_URL", None), \
             patch.object(settings, "REDIS_HOST", "mi-host"), \
             patch.object(settings, "REDIS_PORT", 6380), \
             patch.object(settings, "REDIS_PASSWORD", "secreto"), \
             patch.object(settings, "REDIS_DB", 2), \
             patch.object(settings, "REDIS_SSL", True), \
             patch("redis.asyncio.Redis") as mock_redis_ctor, \
             patch("redis.asyncio.from_url") as mock_from_url, \
             patch("redis.asyncio.RedisCluster") as mock_cluster_ctor:
            redis_module._build_redis_client()

        mock_redis_ctor.assert_called_once()
        self.assertEqual(mock_redis_ctor.call_args.kwargs["host"], "mi-host")
        self.assertEqual(mock_redis_ctor.call_args.kwargs["port"], 6380)
        self.assertEqual(mock_redis_ctor.call_args.kwargs["password"], "secreto")
        self.assertEqual(mock_redis_ctor.call_args.kwargs["db"], 2)
        self.assertTrue(mock_redis_ctor.call_args.kwargs["ssl"])
        mock_from_url.assert_not_called()
        mock_cluster_ctor.assert_not_called()

    def test_sin_cluster_con_url_usa_from_url(self):
        with patch.object(settings, "REDIS_CLUSTER_MODE", False), \
             patch.object(settings, "REDIS_URL", "redis://usuario:pass@elasticache:6379/0"), \
             patch("redis.asyncio.Redis") as mock_redis_ctor, \
             patch("redis.asyncio.from_url") as mock_from_url:
            redis_module._build_redis_client()

        mock_from_url.assert_called_once()
        self.assertEqual(mock_from_url.call_args.args[0], "redis://usuario:pass@elasticache:6379/0")
        mock_redis_ctor.assert_not_called()

    def test_cluster_sin_url_usa_rediscluster_por_host_puerto(self):
        with patch.object(settings, "REDIS_CLUSTER_MODE", True), \
             patch.object(settings, "REDIS_URL", None), \
             patch.object(settings, "REDIS_HOST", "cluster-host"), \
             patch.object(settings, "REDIS_PORT", 6379), \
             patch.object(settings, "REDIS_PASSWORD", "secreto-cluster"), \
             patch.object(settings, "REDIS_SSL", True), \
             patch("redis.asyncio.RedisCluster") as mock_cluster_ctor, \
             patch("redis.asyncio.Redis") as mock_redis_ctor, \
             patch("redis.asyncio.from_url") as mock_from_url:
            redis_module._build_redis_client()

        mock_cluster_ctor.assert_called_once()
        self.assertEqual(mock_cluster_ctor.call_args.kwargs["host"], "cluster-host")
        self.assertEqual(mock_cluster_ctor.call_args.kwargs["port"], 6379)
        self.assertEqual(mock_cluster_ctor.call_args.kwargs["password"], "secreto-cluster")
        self.assertTrue(mock_cluster_ctor.call_args.kwargs["ssl"])
        mock_cluster_ctor.from_url.assert_not_called()
        mock_redis_ctor.assert_not_called()
        mock_from_url.assert_not_called()

    def test_cluster_con_url_usa_rediscluster_from_url(self):
        with patch.object(settings, "REDIS_CLUSTER_MODE", True), \
             patch.object(settings, "REDIS_URL", "redis://usuario:pass@cluster-elasticache:6379/0"), \
             patch("redis.asyncio.RedisCluster") as mock_cluster_ctor:
            redis_module._build_redis_client()

        mock_cluster_ctor.from_url.assert_called_once()
        self.assertEqual(
            mock_cluster_ctor.from_url.call_args.args[0],
            "redis://usuario:pass@cluster-elasticache:6379/0"
        )
        mock_cluster_ctor.assert_not_called()

    def test_common_kwargs_de_resiliencia_se_propagan_siempre(self):
        """decode_responses/timeouts/retry -- las mismas kwargs de resiliencia deben
        llegar sin importar la rama (host/puerto vs. URL, cluster vs. standalone)."""
        with patch.object(settings, "REDIS_CLUSTER_MODE", False), \
             patch.object(settings, "REDIS_URL", None), \
             patch("redis.asyncio.Redis") as mock_redis_ctor:
            redis_module._build_redis_client()

        kwargs = mock_redis_ctor.call_args.kwargs
        self.assertTrue(kwargs["decode_responses"])
        self.assertEqual(kwargs["socket_timeout"], 5.0)
        self.assertEqual(kwargs["socket_connect_timeout"], 5.0)
        self.assertTrue(kwargs["retry_on_timeout"])
        self.assertIn("retry", kwargs)
        self.assertIn("retry_on_error", kwargs)


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


class TestPingRedisMetricaEmf(_RedisModuleStateTestCase):
    """Métrica EMF SSV/RedisHealth (propuesta de observabilidad CX)."""

    async def test_sin_cliente_emite_metrica_resultado_down(self):
        redis_module.redis_client = None
        with patch("app.db.redis.emit_emf_metric") as mock_emit:
            await redis_module.ping_redis()

        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["namespace"], "SSV/RedisHealth")
        self.assertEqual(mock_emit.call_args.kwargs["dimensions"]["resultado"], "down")
        self.assertEqual(mock_emit.call_args.kwargs["metrics"]["redis_up"], (0, "Count"))

    async def test_ping_exitoso_emite_metrica_resultado_up(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        redis_module.redis_client = mock_client
        with patch("app.db.redis.emit_emf_metric") as mock_emit:
            await redis_module.ping_redis()

        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["dimensions"]["resultado"], "up")
        self.assertEqual(mock_emit.call_args.kwargs["metrics"]["redis_up"], (1, "Count"))

    async def test_ping_fallido_emite_metrica_resultado_down(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=ConnectionError("timeout"))
        redis_module.redis_client = mock_client
        with patch("app.db.redis.emit_emf_metric") as mock_emit:
            await redis_module.ping_redis()

        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args.kwargs["dimensions"]["resultado"], "down")
        self.assertEqual(mock_emit.call_args.kwargs["metrics"]["redis_up"], (0, "Count"))


if __name__ == "__main__":
    unittest.main()
