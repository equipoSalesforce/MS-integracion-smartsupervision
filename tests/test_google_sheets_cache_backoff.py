import unittest
from unittest.mock import AsyncMock, patch

from app.core.mapping import SfcSalesforceMapper
from app.core.exceptions import SfcErrorTranslator


class TestGoogleSheetsCacheBackoff(unittest.IsolatedAsyncioTestCase):

    async def test_mapper_cache_backoff_on_google_sheets_outage(self):
        """
        Verifica que ante un fallo HTTP 429 de Google Sheets, SfcSalesforceMapper 
        use los datos locales/RAM y NO intente re-llamar a Google Sheets en peticiones subsiguientes.
        """
        # Forzar estado inicial expirado
        SfcSalesforceMapper.ULTIMA_ACTUALIZACION = 0

        # Mockear _obtener_google_access_token para simular fallo 429 o excepción de Google OAuth
        with patch.object(
            SfcSalesforceMapper, 
            "_obtener_google_access_token", 
            new_callable=AsyncMock, 
            side_effect=Exception("HTTP 429 Too Many Requests")
        ) as mock_oauth:
            
            # Petición 1: Debe intentar Google Sheets, fallar y hacer fallback
            await SfcSalesforceMapper.obtener_catalogos_y_mapeos()
            self.assertEqual(mock_oauth.call_count, 1)
            self.assertGreater(SfcSalesforceMapper.ULTIMA_ACTUALIZACION, 0)

            # Petición 2 (Inmediata): NO debe intentar llamar a Google Sheets de nuevo
            await SfcSalesforceMapper.obtener_catalogos_y_mapeos()
            self.assertEqual(
                mock_oauth.call_count, 1, 
                "El mapper intentó volver a llamar a Google Sheets a pesar de estar en ventana de caché."
            )

    async def test_translator_cache_backoff_on_google_sheets_outage(self):
        """
        Verifica que ante un fallo de Google Sheets, SfcErrorTranslator mantenga la matriz en RAM
        y NO vuelva a intentar llamar a Google Sheets en peticiones subsiguientes.
        """
        SfcErrorTranslator.ULTIMA_ACTUALIZACION = 0

        with patch.object(
            SfcErrorTranslator, 
            "_obtener_google_access_token", 
            new_callable=AsyncMock, 
            side_effect=Exception("HTTP 429 Too Many Requests")
        ) as mock_oauth:
            
            # Petición 1: Debe intentar Google Sheets, fallar y hacer fallback
            reglas_1 = await SfcErrorTranslator.obtener_matriz_errores()
            self.assertEqual(mock_oauth.call_count, 1)
            self.assertGreater(SfcErrorTranslator.ULTIMA_ACTUALIZACION, 0)
            self.assertTrue(len(reglas_1) > 0)

            # Petición 2 (Inmediata): Debe usar la matriz en RAM sin llamar a Google Sheets
            await SfcErrorTranslator.obtener_matriz_errores()
            self.assertEqual(
                mock_oauth.call_count, 1, 
                "El traductor intentó volver a llamar a Google Sheets a pesar de estar en ventana de caché."
            )


if __name__ == "__main__":
    unittest.main()