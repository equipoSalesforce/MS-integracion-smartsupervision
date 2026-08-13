# tests/test_single_flight_refresh.py
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from app.core.mapping import SfcSalesforceMapper
from app.core.exceptions import SfcErrorTranslator


class TestSingleFlightRefresh(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Reiniciar estados de caché
        SfcSalesforceMapper.CATALOGOS = {}
        SfcSalesforceMapper.ULTIMA_ACTUALIZACION = 0
        SfcSalesforceMapper._REFRESH_LOCK = None

        SfcErrorTranslator.MATRIZ_ERRORES_TEXTO = []
        SfcErrorTranslator.ULTIMA_ACTUALIZACION = 0
        SfcErrorTranslator._REFRESH_LOCK = None

    @patch("app.core.mapping.SfcSalesforceMapper._obtener_google_access_token")
    async def test_mapper_single_flight_solo_ejecuta_un_fetch_concurrente(self, mock_oauth):
        """Verifica que 10 llamadas concurrentes a obtener_catalogos_y_mapeos ejecuten solo 1 consulta a OAuth/Sheets."""
        mock_oauth.side_effect = lambda *args, **kwargs: asyncio.sleep(0.05) or "mock_token"

        # Disparar 10 solicitudes de refresco simultáneas
        await asyncio.gather(*[
            SfcSalesforceMapper.obtener_catalogos_y_mapeos() for _ in range(10)
        ])

        # Se debe haber ejecutado exactamente 1 llamada gracias al bloqueo Single-Flight
        self.assertEqual(mock_oauth.call_count, 1)

    @patch("app.core.exceptions.SfcErrorTranslator._obtener_google_access_token")
    async def test_translator_single_flight_solo_ejecuta_un_fetch_concurrente(self, mock_oauth):
        """Verifica que 10 llamadas concurrentes a obtener_matriz_errores ejecuten solo 1 consulta a OAuth/Sheets."""
        mock_oauth.side_effect = lambda *args, **kwargs: asyncio.sleep(0.05) or "mock_token"

        await asyncio.gather(*[
            SfcErrorTranslator.obtener_matriz_errores() for _ in range(10)
        ])

        self.assertEqual(mock_oauth.call_count, 1)


if __name__ == "__main__":
    unittest.main()