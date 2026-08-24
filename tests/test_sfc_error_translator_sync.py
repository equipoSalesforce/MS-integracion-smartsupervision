# tests/test_sfc_error_translator_sync.py
"""
Cobertura de los internos de sincronización de SfcErrorTranslator con Google
Sheets/OAuth no cubiertos por test_sfc_error_translator.py (que sólo prueba
_extraer_informacion_error/procesar_y_lanzar con la matriz ya mockeada):
_obtener_google_access_token sin credenciales, cargar_matriz_local con un
archivo corrupto/inaccesible, la alerta de antigüedad crítica en
_manejar_falla_sincronizacion_matriz, _refrescar_matriz_desde_sheets sin
access token, y _notificar_desconocido_async sin un event loop activo.
"""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.core.exceptions import SfcErrorTranslator


class TestObtenerGoogleAccessTokenSinCredenciales(unittest.IsolatedAsyncioTestCase):

    async def test_sin_credenciales_retorna_none(self):
        with patch("app.core.exceptions.settings") as mock_settings:
            mock_settings.GOOGLE_CLIENT_ID = None
            mock_settings.GOOGLE_CLIENT_SECRET = None
            mock_settings.GOOGLE_REFRESH_TOKEN = None

            resultado = await SfcErrorTranslator._obtener_google_access_token()

        self.assertIsNone(resultado)


class TestCargarMatrizLocal(unittest.TestCase):

    def setUp(self):
        self.matriz_original = SfcErrorTranslator.MATRIZ_ERRORES_TEXTO

    def tearDown(self):
        SfcErrorTranslator.MATRIZ_ERRORES_TEXTO = self.matriz_original

    def test_archivo_corrupto_o_inaccesible_deja_matriz_vacia_sin_lanzar(self):
        with patch("builtins.open", side_effect=OSError("archivo no encontrado")):
            SfcErrorTranslator.cargar_matriz_local()  # No debe lanzar.

        self.assertEqual(SfcErrorTranslator.MATRIZ_ERRORES_TEXTO, [])


class TestManejarFallaSincronizacionMatriz(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.exito_original = SfcErrorTranslator.ULTIMO_EXITO_TIMESTAMP

    def tearDown(self):
        SfcErrorTranslator.ULTIMO_EXITO_TIMESTAMP = self.exito_original

    async def test_antiguedad_supera_el_umbral_dispara_alerta_critica(self):
        ahora = time.time()
        SfcErrorTranslator.ULTIMO_EXITO_TIMESTAMP = ahora - SfcErrorTranslator.MAX_STALE_TTL_SEGUNDOS - 3600

        with patch("app.services.email_service.EmailAlertService.notificar_catalogo_stale", new_callable=AsyncMock) as mock_alert:
            await SfcErrorTranslator._manejar_falla_sincronizacion_matriz(Exception("Sheets caido"), ahora)
            await asyncio.sleep(0)

        mock_alert.assert_awaited_once()

    async def test_sin_exito_previo_no_dispara_alerta(self):
        SfcErrorTranslator.ULTIMO_EXITO_TIMESTAMP = 0

        with patch("app.services.email_service.EmailAlertService.notificar_catalogo_stale", new_callable=AsyncMock) as mock_alert:
            await SfcErrorTranslator._manejar_falla_sincronizacion_matriz(Exception("Sheets caido"), time.time())

        mock_alert.assert_not_called()


class TestRefrescarMatrizDesdeSheets(unittest.IsolatedAsyncioTestCase):

    async def test_sin_access_token_retorna_false(self):
        with patch.object(SfcErrorTranslator, "_obtener_google_access_token", new_callable=AsyncMock, return_value=None):
            resultado = await SfcErrorTranslator._refrescar_matriz_desde_sheets("sheet-id", "A:C", time.time())

        self.assertFalse(resultado)


class TestNotificarDesconocidoAsync(unittest.TestCase):
    """Se llama desde código síncrono (sin un event loop corriendo) -- distinto de
    los demás tests de este módulo, que sí corren dentro de uno."""

    def test_sin_event_loop_activo_no_lanza(self):
        SfcErrorTranslator._notificar_desconocido_async(500, "error de prueba", None)  # No debe lanzar.


if __name__ == "__main__":
    unittest.main()
