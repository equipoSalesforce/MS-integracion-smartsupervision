# tests/test_worker_process.py
"""
Cobertura de app/worker.py: heartbeat de liveness, healthcheck activo de Redis,
y el ciclo de arranque/apagado de run_worker_process(). Antes 51%.
"""
import asyncio
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

import app.worker as worker_module
from app.core.config import settings


class TestTouchHeartbeat(unittest.TestCase):

    def test_escribe_timestamp_sin_lanzar(self):
        m_open = unittest.mock.mock_open()
        with patch("builtins.open", m_open):
            worker_module._touch_heartbeat()
        m_open.assert_called_once_with(worker_module.HEARTBEAT_FILE, "w")
        m_open().write.assert_called_once()

    def test_error_al_escribir_no_propaga(self):
        with patch("builtins.open", side_effect=OSError("disco lleno")):
            worker_module._touch_heartbeat()  # No debe lanzar.


class TestCheckRedisHealth(unittest.IsolatedAsyncioTestCase):

    async def test_sin_cliente_retorna_false(self):
        with patch("app.worker.get_redis_client", return_value=None):
            self.assertFalse(await worker_module._check_redis_health())

    async def test_ping_true_retorna_true(self):
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(return_value=True)
        with patch("app.worker.get_redis_client", return_value=mock_redis):
            self.assertTrue(await worker_module._check_redis_health())

    async def test_ping_pong_string_retorna_true(self):
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(return_value="PONG")
        with patch("app.worker.get_redis_client", return_value=mock_redis):
            self.assertTrue(await worker_module._check_redis_health())

    async def test_ping_lanza_excepcion_retorna_false(self):
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("caído"))
        with patch("app.worker.get_redis_client", return_value=mock_redis):
            self.assertFalse(await worker_module._check_redis_health())


class TestHeartbeatLoop(unittest.IsolatedAsyncioTestCase):
    """
    HEARTBEAT_INTERVAL_SECONDS se baja a 0 durante estos tests: entre cada
    iteración del loop, _heartbeat_loop espera ese intervalo real vía
    asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
    -- con el valor real (30s) cualquier test que necesite varias iteraciones
    (ej. alcanzar el umbral de 10 fallos) tardaría minutos en vez de milisegundos.
    """

    async def asyncSetUp(self):
        self._interval_patch = patch.object(worker_module, "HEARTBEAT_INTERVAL_SECONDS", 0)
        self._interval_patch.start()

    async def asyncTearDown(self):
        self._interval_patch.stop()

    async def test_iteracion_exitosa_actualiza_heartbeat_sin_alertar(self):
        stop_event = asyncio.Event()

        async def _check_una_vez_y_parar():
            stop_event.set()
            return True

        with patch("app.worker._check_redis_health", side_effect=_check_una_vez_y_parar), \
             patch("app.worker._touch_heartbeat") as mock_touch, \
             patch("app.worker.EmailAlertService.notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alert:
            await worker_module._heartbeat_loop(stop_event)

        mock_touch.assert_called_once()
        mock_alert.assert_not_called()

    async def test_alerta_tras_alcanzar_el_umbral_de_fallos_consecutivos(self):
        stop_event = asyncio.Event()
        contador = {"n": 0}

        async def _fallar_hasta_umbral():
            contador["n"] += 1
            if contador["n"] >= worker_module.REDIS_FALLOS_CONSECUTIVOS_PARA_ALERTAR:
                stop_event.set()
            return False

        with patch("app.worker._check_redis_health", side_effect=_fallar_hasta_umbral), \
             patch("app.worker._touch_heartbeat"), \
             patch("app.worker.EmailAlertService.notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alert:
            await worker_module._heartbeat_loop(stop_event)

        mock_alert.assert_awaited_once()
        self.assertEqual(contador["n"], worker_module.REDIS_FALLOS_CONSECUTIVOS_PARA_ALERTAR)

    async def test_no_alerta_dos_veces_seguidas_una_vez_notificado(self):
        stop_event = asyncio.Event()
        contador = {"n": 0}
        umbral = worker_module.REDIS_FALLOS_CONSECUTIVOS_PARA_ALERTAR

        async def _fallar_mas_alla_del_umbral():
            contador["n"] += 1
            if contador["n"] > umbral + 2:
                stop_event.set()
            return False

        with patch("app.worker._check_redis_health", side_effect=_fallar_mas_alla_del_umbral), \
             patch("app.worker._touch_heartbeat"), \
             patch("app.worker.EmailAlertService.notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alert:
            await worker_module._heartbeat_loop(stop_event)

        mock_alert.assert_awaited_once()  # Sigue fallando pero ya no vuelve a notificar.

    async def test_recuperacion_resetea_el_contador_de_fallos(self):
        stop_event = asyncio.Event()
        secuencia = iter([False, False, True])

        async def _secuencia_controlada():
            try:
                resultado = next(secuencia)
            except StopIteration:
                stop_event.set()
                return True
            if resultado:
                stop_event.set()
            return resultado

        with patch("app.worker._check_redis_health", side_effect=_secuencia_controlada), \
             patch("app.worker._touch_heartbeat"), \
             patch("app.worker.EmailAlertService.notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alert:
            await worker_module._heartbeat_loop(stop_event)

        mock_alert.assert_not_called()  # 2 fallos no alcanzan el umbral (10).


class TestRunWorkerProcess(unittest.IsolatedAsyncioTestCase):

    async def test_arranca_y_limpia_recursos_al_cancelar(self):
        run_scheduler_original = settings.RUN_SCHEDULER
        try:
            with patch("app.worker.init_redis", new_callable=AsyncMock) as mock_init_redis, \
                 patch("app.worker.SfcErrorTranslator.obtener_matriz_errores", new_callable=AsyncMock), \
                 patch("app.worker.SfcSalesforceMapper.obtener_catalogos_y_mapeos", new_callable=AsyncMock), \
                 patch("app.worker.iniciar_scheduler") as mock_iniciar_sched, \
                 patch("app.worker.detener_scheduler", new_callable=AsyncMock) as mock_detener_sched, \
                 patch("app.worker.close_redis", new_callable=AsyncMock) as mock_close_redis, \
                 patch("app.worker.close_crm_webhook_client", new_callable=AsyncMock) as mock_close_crm, \
                 patch("app.worker.EmailAlertService.shutdown", new_callable=AsyncMock) as mock_email_shutdown, \
                 patch("app.worker._auth_manager_instance") as mock_auth_mgr, \
                 patch("app.worker._heartbeat_loop", new_callable=AsyncMock), \
                 patch("app.worker._touch_heartbeat"):
                mock_auth_mgr.close = AsyncMock()

                task = asyncio.create_task(worker_module.run_worker_process())
                await asyncio.sleep(0.05)  # deja que arranque y llegue a stop_event.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                mock_init_redis.assert_awaited_once()
                mock_iniciar_sched.assert_called_once()
                self.assertTrue(settings.RUN_SCHEDULER)
                mock_detener_sched.assert_awaited_once()
                mock_email_shutdown.assert_awaited_once()
                mock_close_redis.assert_awaited_once()
                mock_close_crm.assert_awaited_once()
                mock_auth_mgr.close.assert_awaited_once()
        finally:
            settings.RUN_SCHEDULER = run_scheduler_original

    async def test_fallo_precargando_catalogos_cierra_redis_y_relanza(self):
        with patch("app.worker.init_redis", new_callable=AsyncMock), \
             patch("app.worker.SfcErrorTranslator.obtener_matriz_errores", new_callable=AsyncMock, side_effect=RuntimeError("Google Sheets caído")), \
             patch("app.worker.close_redis", new_callable=AsyncMock) as mock_close_redis:
            with self.assertRaises(RuntimeError):
                await worker_module.run_worker_process()

        mock_close_redis.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
