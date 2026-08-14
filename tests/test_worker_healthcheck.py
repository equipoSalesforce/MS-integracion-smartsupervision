import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import app.worker as worker
from app.worker import _check_redis_health
from app.services.email_service import EmailAlertService


class TestWorkerRedisHealthcheck(unittest.IsolatedAsyncioTestCase):

    @patch("app.worker.get_redis_client")
    async def test_check_redis_health_pong_exitoso(self, mock_get_redis):
        """Verifica que si Redis responde PONG, la prueba retorne True."""
        redis_mock = AsyncMock()
        redis_mock.ping.return_value = "PONG"
        mock_get_redis.return_value = redis_mock

        res = await _check_redis_health()
        self.assertTrue(res)

    @patch("app.worker.get_redis_client")
    async def test_check_redis_health_excepcion_falla(self, mock_get_redis):
        """Verifica que si Redis lanza una excepción de conexión, la prueba retorne False."""
        redis_mock = AsyncMock()
        redis_mock.ping.side_effect = Exception("Connection refused / Timeout")
        mock_get_redis.return_value = redis_mock

        res = await _check_redis_health()
        self.assertFalse(res)

    async def test_heartbeat_loop_no_se_congela_si_redis_esta_caido(self):
        """
        P1-14: el heartbeat de liveness (usado por Docker/ECS para decidir si
        reiniciar el contenedor) debe seguir actualizándose aunque Redis esté
        caído, para no forzar un reinicio masivo de todas las réplicas del
        worker por una dependencia externa caída (que un reinicio no soluciona).
        """
        mock_touch = MagicMock()

        with patch.object(worker, "_check_redis_health", AsyncMock(return_value=False)), \
             patch.object(worker, "_touch_heartbeat", mock_touch), \
             patch.object(worker, "HEARTBEAT_INTERVAL_SECONDS", 0.01), \
             patch.object(worker, "REDIS_FALLOS_CONSECUTIVOS_PARA_ALERTAR", 3), \
             patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alerta:

            stop_event = asyncio.Event()
            task = asyncio.create_task(worker._heartbeat_loop(stop_event))
            await asyncio.sleep(0.2)
            stop_event.set()
            await task

        # El heartbeat se sigue tocando en cada ciclo pese a que Redis falla siempre.
        self.assertGreaterEqual(mock_touch.call_count, 3)
        # Tras suficientes fallos consecutivos, se alerta por correo (una sola vez).
        mock_alerta.assert_called_once()

    async def test_heartbeat_loop_se_recupera_sin_reenviar_alerta(self):
        """Si Redis se recupera, el ciclo deja de contar fallos y no re-alerta al volver a fallar por debajo del umbral."""
        mock_touch = MagicMock()
        resultados = [False, False, False, True, False]

        async def _check_secuencial():
            return resultados.pop(0) if resultados else True

        with patch.object(worker, "_check_redis_health", _check_secuencial), \
             patch.object(worker, "_touch_heartbeat", mock_touch), \
             patch.object(worker, "HEARTBEAT_INTERVAL_SECONDS", 0.01), \
             patch.object(worker, "REDIS_FALLOS_CONSECUTIVOS_PARA_ALERTAR", 3), \
             patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alerta:

            stop_event = asyncio.Event()
            task = asyncio.create_task(worker._heartbeat_loop(stop_event))
            await asyncio.sleep(0.2)
            stop_event.set()
            await task

        # Se alertó exactamente una vez (al 3er fallo consecutivo), no de nuevo tras el
        # siguiente fallo aislado posterior a la recuperación.
        mock_alerta.assert_called_once()


if __name__ == "__main__":
    unittest.main()