# tests/test_main_lifespan_recursos.py
"""
Cobertura de app/main.py::_iniciar_recursos_globales / _detener_recursos_globales
-- el arranque/apagado de recursos globales del lifespan de FastAPI (Redis,
scheduler, catalogos/matriz, clientes HTTP). Antes sin cobertura directa de
las ramas de error (cada paso se aisla en su propio try/except para que un
fallo puntual no impida el resto del arranque/apagado).
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI

import app.main as main_module
from app.core.config import settings


class TestIniciarRecursosGlobales(unittest.IsolatedAsyncioTestCase):

    async def _iniciar_con_mocks(self, **overrides):
        app = FastAPI()
        defaults = dict(
            init_redis=AsyncMock(return_value=True),
            iniciar_scheduler=lambda: None,
            obtener_matriz_errores=AsyncMock(),
            obtener_catalogos_y_mapeos=AsyncMock(),
        )
        defaults.update(overrides)
        with patch("app.main.init_redis", defaults["init_redis"]), \
             patch("app.main.iniciar_scheduler", defaults["iniciar_scheduler"]), \
             patch("app.main.SfcErrorTranslator.obtener_matriz_errores", defaults["obtener_matriz_errores"]), \
             patch("app.main.SfcSalesforceMapper.obtener_catalogos_y_mapeos", defaults["obtener_catalogos_y_mapeos"]):
            try:
                await main_module._iniciar_recursos_globales(app)
            finally:
                if hasattr(app.state, "http_client"):
                    await app.state.http_client.aclose()
        return app

    async def test_arranque_exitoso_con_scheduler_habilitado(self):
        with patch.object(settings, "RUN_SCHEDULER", True):
            mock_iniciar_sched = MagicMock()
            app = await self._iniciar_con_mocks(iniciar_scheduler=mock_iniciar_sched)
        mock_iniciar_sched.assert_called_once()
        self.assertTrue(hasattr(app.state, "http_client"))

    async def test_arranque_con_scheduler_deshabilitado_no_lo_inicia(self):
        with patch.object(settings, "RUN_SCHEDULER", False), \
             patch("app.main.iniciar_scheduler") as mock_iniciar_sched:
            await self._iniciar_con_mocks(iniciar_scheduler=mock_iniciar_sched)
        mock_iniciar_sched.assert_not_called()

    async def test_fallo_al_iniciar_redis_no_detiene_el_arranque(self):
        with patch.object(settings, "RUN_SCHEDULER", False):
            app = await self._iniciar_con_mocks(init_redis=AsyncMock(side_effect=ConnectionError("redis caido")))
        # Llega hasta el final (http_client sigue quedando inicializado) pese al fallo de Redis.
        self.assertTrue(hasattr(app.state, "http_client"))

    async def test_fallo_al_iniciar_scheduler_no_detiene_el_arranque(self):
        mock_iniciar_sched = MagicMock(side_effect=RuntimeError("no se pudo arrancar"))
        with patch.object(settings, "RUN_SCHEDULER", True):
            app = await self._iniciar_con_mocks(iniciar_scheduler=mock_iniciar_sched)
        mock_iniciar_sched.assert_called_once()
        self.assertTrue(hasattr(app.state, "http_client"))

    async def test_fallo_precargando_catalogos_relanza(self):
        with patch.object(settings, "RUN_SCHEDULER", False):
            with self.assertRaises(RuntimeError):
                await self._iniciar_con_mocks(
                    obtener_matriz_errores=AsyncMock(side_effect=RuntimeError("Google Sheets caido"))
                )


class TestDetenerRecursosGlobales(unittest.IsolatedAsyncioTestCase):

    async def _detener_con_mocks(self, **overrides):
        app = FastAPI()
        defaults = dict(
            detener_scheduler=AsyncMock(),
            close_redis=AsyncMock(),
            close_crm_webhook_client=AsyncMock(),
            auth_manager_close=AsyncMock(),
            email_shutdown=AsyncMock(),
        )
        defaults.update(overrides)
        with patch("app.main.detener_scheduler", defaults["detener_scheduler"]), \
             patch("app.main.close_redis", defaults["close_redis"]), \
             patch("app.main.close_crm_webhook_client", defaults["close_crm_webhook_client"]), \
             patch("app.main._auth_manager_instance") as mock_auth_mgr, \
             patch("app.main.EmailAlertService.shutdown", defaults["email_shutdown"]):
            mock_auth_mgr.close = defaults["auth_manager_close"]
            await main_module._detener_recursos_globales(app)
        return defaults

    async def test_apagado_exitoso_llama_a_todos_los_pasos(self):
        defaults = await self._detener_con_mocks()
        defaults["detener_scheduler"].assert_awaited_once()
        defaults["close_redis"].assert_awaited_once()
        defaults["close_crm_webhook_client"].assert_awaited_once()
        defaults["auth_manager_close"].assert_awaited_once()
        defaults["email_shutdown"].assert_awaited_once()

    async def test_fallo_deteniendo_scheduler_no_impide_el_resto_del_apagado(self):
        defaults = await self._detener_con_mocks(
            detener_scheduler=AsyncMock(side_effect=RuntimeError("scheduler colgado"))
        )
        defaults["close_redis"].assert_awaited_once()
        defaults["close_crm_webhook_client"].assert_awaited_once()
        defaults["auth_manager_close"].assert_awaited_once()

    async def test_fallo_cerrando_redis_no_impide_el_resto_del_apagado(self):
        defaults = await self._detener_con_mocks(close_redis=AsyncMock(side_effect=ConnectionError("caido")))
        defaults["close_crm_webhook_client"].assert_awaited_once()
        defaults["auth_manager_close"].assert_awaited_once()

    async def test_fallo_cerrando_crm_webhook_client_no_impide_el_resto(self):
        defaults = await self._detener_con_mocks(
            close_crm_webhook_client=AsyncMock(side_effect=RuntimeError("socket ya cerrado"))
        )
        defaults["auth_manager_close"].assert_awaited_once()
        defaults["email_shutdown"].assert_awaited_once()

    async def test_fallo_cerrando_auth_manager_no_impide_el_resto(self):
        defaults = await self._detener_con_mocks(auth_manager_close=AsyncMock(side_effect=RuntimeError("fallo")))
        defaults["email_shutdown"].assert_awaited_once()

    async def test_sin_http_client_en_app_state_no_lanza(self):
        # app.state.http_client nunca se creó (ej. si _iniciar_recursos_globales no corrió) --
        # hasattr(app.state, "http_client") debe ser False y ese bloque se omite sin lanzar.
        await self._detener_con_mocks()  # El FastAPI() fresco no tiene http_client -- no debe lanzar.


if __name__ == "__main__":
    unittest.main()
