import unittest
from unittest.mock import AsyncMock, patch
from app.worker import _check_redis_health


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


if __name__ == "__main__":
    unittest.main()