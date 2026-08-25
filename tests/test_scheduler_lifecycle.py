# tests/test_scheduler_lifecycle.py
"""
Cobertura del ciclo de vida del scheduler (app/workers/scheduler.py): el
cliente HTTP perezoso compartido, el job de purga nocturna, y el
arranque/apagado de APScheduler. Antes sin cobertura directa -- se mockea el
singleton `scheduler` real en vez de arrancarlo de verdad (evita dejar un
AsyncIOScheduler corriendo en segundo plano entre tests).
"""
import asyncio
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

import app.workers.scheduler as scheduler_module
from app.workers.scheduler import purgar_cola_job, iniciar_scheduler, detener_scheduler, SchedulerJobLock
from app.core.config import settings


class TestSchedulerHttpClient(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._orig_client = scheduler_module._scheduler_http_client
        scheduler_module._scheduler_http_client = None

    async def asyncTearDown(self):
        await scheduler_module._close_scheduler_http_client()
        scheduler_module._scheduler_http_client = self._orig_client

    def test_crea_cliente_perezosamente_y_lo_reutiliza(self):
        c1 = scheduler_module._get_scheduler_http_client()
        c2 = scheduler_module._get_scheduler_http_client()
        self.assertIs(c1, c2)

    async def test_crea_uno_nuevo_si_el_anterior_esta_cerrado(self):
        c1 = scheduler_module._get_scheduler_http_client()
        await c1.aclose()
        c2 = scheduler_module._get_scheduler_http_client()
        self.assertIsNot(c1, c2)

    async def test_close_sin_cliente_previo_no_lanza(self):
        await scheduler_module._close_scheduler_http_client()
        self.assertIsNone(scheduler_module._scheduler_http_client)

    async def test_close_deja_el_cliente_cerrado_y_en_none(self):
        cliente = scheduler_module._get_scheduler_http_client()
        await scheduler_module._close_scheduler_http_client()
        self.assertTrue(cliente.is_closed)
        self.assertIsNone(scheduler_module._scheduler_http_client)


class TestPurgarColaJob(unittest.IsolatedAsyncioTestCase):

    async def test_sin_redis_no_hace_nada(self):
        with patch("app.workers.scheduler.get_redis_client", return_value=None), \
             patch.object(SchedulerJobLock, "acquire", new_callable=AsyncMock) as mock_acquire:
            await purgar_cola_job()
        mock_acquire.assert_not_called()

    async def test_lock_no_adquirido_no_ejecuta_la_purga(self):
        with patch("app.workers.scheduler.get_redis_client", return_value=MagicMock()), \
             patch.object(SchedulerJobLock, "acquire", new_callable=AsyncMock, return_value=False), \
             patch("app.services.queue_service.QueueService.purgar_registros_antiguos", new_callable=AsyncMock) as mock_purge:
            await purgar_cola_job()
        mock_purge.assert_not_called()

    async def test_ejecuta_la_purga_y_libera_el_lock(self):
        with patch("app.workers.scheduler.get_redis_client", return_value=MagicMock()), \
             patch.object(SchedulerJobLock, "acquire", new_callable=AsyncMock, return_value=True), \
             patch.object(SchedulerJobLock, "release", new_callable=AsyncMock) as mock_release, \
             patch("app.services.queue_service.QueueService.purgar_registros_antiguos", new_callable=AsyncMock) as mock_purge:
            await purgar_cola_job()
        mock_purge.assert_awaited_once_with(
            dias_retencion=settings.QUEUE_RETENTION_DAYS,
            dias_retencion_dlq=settings.QUEUE_RETENTION_DAYS_DLQ
        )
        mock_release.assert_awaited_once()

    async def test_libera_el_lock_incluso_si_la_purga_falla(self):
        with patch("app.workers.scheduler.get_redis_client", return_value=MagicMock()), \
             patch.object(SchedulerJobLock, "acquire", new_callable=AsyncMock, return_value=True), \
             patch.object(SchedulerJobLock, "release", new_callable=AsyncMock) as mock_release, \
             patch(
                 "app.services.queue_service.QueueService.purgar_registros_antiguos",
                 new_callable=AsyncMock, side_effect=RuntimeError("redis caído a mitad de la purga")
             ):
            with self.assertRaises(RuntimeError):
                await purgar_cola_job()
        mock_release.assert_awaited_once()


class TestIniciarScheduler(unittest.TestCase):

    def test_agrega_los_dos_jobs_y_arranca(self):
        mock_scheduler = MagicMock()
        mock_scheduler.running = False
        with patch.object(settings, "QUEUE_ENABLED", True), \
             patch("app.workers.scheduler.scheduler", mock_scheduler):
            iniciar_scheduler()

        self.assertEqual(mock_scheduler.add_job.call_count, 2)
        mock_scheduler.start.assert_called_once()

    def test_purga_usa_timezone_america_bogota(self):
        """
        🔴 FIX (hallazgo de revisión externa, 2026-08-25): sin `timezone=` explícito,
        el cron de purga corría en la zona horaria local del contenedor (lo que
        APScheduler detecte vía tzlocal) -- no necesariamente UTC como decía el log,
        y desde luego no America/Bogota como el resto del dominio.
        """
        mock_scheduler = MagicMock()
        mock_scheduler.running = False
        with patch.object(settings, "QUEUE_ENABLED", True), \
             patch("app.workers.scheduler.scheduler", mock_scheduler):
            iniciar_scheduler()

        llamada_purga = next(
            c for c in mock_scheduler.add_job.call_args_list
            if c.kwargs.get("id") == "sfc_queue_purge_job"
        )
        self.assertEqual(str(llamada_purga.kwargs["timezone"]), "America/Bogota")

    def test_no_hace_nada_si_la_cola_esta_deshabilitada(self):
        mock_scheduler = MagicMock()
        mock_scheduler.running = False
        with patch.object(settings, "QUEUE_ENABLED", False), \
             patch("app.workers.scheduler.scheduler", mock_scheduler):
            iniciar_scheduler()

        mock_scheduler.add_job.assert_not_called()
        mock_scheduler.start.assert_not_called()

    def test_no_hace_nada_si_ya_esta_corriendo(self):
        mock_scheduler = MagicMock()
        mock_scheduler.running = True
        with patch.object(settings, "QUEUE_ENABLED", True), \
             patch("app.workers.scheduler.scheduler", mock_scheduler):
            iniciar_scheduler()

        mock_scheduler.add_job.assert_not_called()


class TestDetenerScheduler(unittest.IsolatedAsyncioTestCase):

    async def test_no_corriendo_solo_cierra_el_http_client(self):
        mock_scheduler = MagicMock()
        mock_scheduler.running = False
        with patch("app.workers.scheduler.scheduler", mock_scheduler), \
             patch("app.workers.scheduler._close_scheduler_http_client", new_callable=AsyncMock) as mock_close:
            await detener_scheduler()

        mock_close.assert_awaited_once()
        mock_scheduler.shutdown.assert_not_called()

    async def test_corriendo_sin_jobs_en_vuelo_apaga_de_inmediato(self):
        mock_scheduler = MagicMock()
        mock_scheduler.running = True
        mock_executor = MagicMock()
        mock_executor._pending_futures = ()
        mock_scheduler._lookup_executor.return_value = mock_executor

        with patch("app.workers.scheduler.scheduler", mock_scheduler), \
             patch("app.workers.scheduler._close_scheduler_http_client", new_callable=AsyncMock):
            await detener_scheduler()

        mock_scheduler.shutdown.assert_called_once_with(wait=False)

    async def test_espera_jobs_en_vuelo_antes_de_apagar(self):
        mock_scheduler = MagicMock()
        mock_scheduler.running = True

        async def _job_rapido():
            await asyncio.sleep(0.01)

        tarea = asyncio.create_task(_job_rapido())
        mock_executor = MagicMock()
        mock_executor._pending_futures = (tarea,)
        mock_scheduler._lookup_executor.return_value = mock_executor

        with patch("app.workers.scheduler.scheduler", mock_scheduler), \
             patch("app.workers.scheduler._close_scheduler_http_client", new_callable=AsyncMock):
            await detener_scheduler(timeout_segundos=1.0)

        self.assertTrue(tarea.done())
        mock_scheduler.shutdown.assert_called_once_with(wait=False)

    async def test_fallo_inspeccionando_jobs_en_vuelo_no_impide_el_apagado(self):
        mock_scheduler = MagicMock()
        mock_scheduler.running = True
        mock_scheduler._lookup_executor.side_effect = RuntimeError("no default executor")

        with patch("app.workers.scheduler.scheduler", mock_scheduler), \
             patch("app.workers.scheduler._close_scheduler_http_client", new_callable=AsyncMock):
            await detener_scheduler()

        mock_scheduler.shutdown.assert_called_once_with(wait=False)


if __name__ == "__main__":
    unittest.main()
