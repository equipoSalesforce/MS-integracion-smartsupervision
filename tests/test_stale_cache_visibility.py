# tests/test_stale_cache_visibility.py
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch
from app.core.mapping import SfcSalesforceMapper


class TestStaleCacheVisibility(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        SfcSalesforceMapper.CATALOGOS = {"tipo_id": {"1": "CC"}}
        SfcSalesforceMapper.INVERSE_CATALOGS = {"tipo_id": {"cc": 1}}
        SfcSalesforceMapper._REFRESH_LOCK = None

    @patch("app.core.mapping.SfcSalesforceMapper._obtener_google_access_token")
    @patch("app.services.email_service.EmailAlertService.notificar_catalogo_stale")
    async def test_fallo_refresh_con_cache_stale_mayor_a_24h_dispara_alerta(self, mock_alerta, mock_oauth):
        """
        Verifica que si el refresco de catálogos falla y la caché previa en RAM
        tiene más de 24 horas de antigüedad (> 86400s), se emita una alerta crítica.
        """
        # Simulamos que la última actualización ocurrió hace 25 horas (90000 segundos)
        SfcSalesforceMapper.ULTIMA_ACTUALIZACION = time.time() - 90000
        SfcSalesforceMapper.CACHE_TTL_SEGUNDOS = 600

        # Simulamos fallo de red al refrescar
        mock_oauth.side_effect = Exception("Google OAuth API Timeout / 503")

        with patch("app.core.config.settings.GOOGLE_CATALOGS_SPREADSHEET_ID", "sheet-123"):
            await SfcSalesforceMapper.obtener_catalogos_y_mapeos()

        # Permitir la ejecución del task asíncrono
        await asyncio.sleep(0.05)

        # Verificamos que la alerta por correo fue disparada por superar el umbral de 24 horas
        mock_alerta.assert_called_once()
        args_alerta = mock_alerta.call_args[1]
        self.assertIn("SfcSalesforceMapper", args_alerta["nombre_componente"])
        self.assertGreater(args_alerta["edad_horas"], 24.0)


if __name__ == "__main__":
    unittest.main()