# tests/test_mapping_refresco_periodico.py
"""
Cobertura de SfcSalesforceMapper.iniciar_refresco_periodico/_loop_refresco_
periodico/detener_refresco_periodico -- hallazgo de revisión, 2026-08-26.

refrescar_catalogos_job (antes en scheduler.py) sólo corría en el proceso con
RUN_SCHEDULER activo -- en producción, únicamente el worker
(scripts/render_task_def.py fija RUN_SCHEDULER="False" incondicionalmente para
el servicio API). Los catálogos en RAM de cada réplica de la API quedaban
congelados en lo que cargó al arrancar, sin refresco nunca más, mientras el
worker sí refrescaba cada CACHE_TTL_SEGUNDOS -- exactamente la divergencia que
el hallazgo C1 debía cerrar, pero nunca llegó a la API real.

Este mecanismo reemplaza ese job: corre por proceso (API y worker), sin lock
cross-proceso -- cada proceso tiene su propia copia de CATALOGOS en RAM y
necesita refrescarla independientemente.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.core.mapping import SfcSalesforceMapper


class TestIniciarRefrescoPeriodico(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._orig_task = SfcSalesforceMapper._refresh_task
        SfcSalesforceMapper._refresh_task = None

    async def asyncTearDown(self):
        if SfcSalesforceMapper._refresh_task is not None:
            SfcSalesforceMapper._refresh_task.cancel()
            try:
                await SfcSalesforceMapper._refresh_task
            except asyncio.CancelledError:
                pass
        SfcSalesforceMapper._refresh_task = self._orig_task

    async def test_arranca_una_tarea_en_segundo_plano(self):
        await SfcSalesforceMapper.iniciar_refresco_periodico()
        self.assertIsNotNone(SfcSalesforceMapper._refresh_task)
        self.assertFalse(SfcSalesforceMapper._refresh_task.done())

    async def test_llamar_dos_veces_no_crea_una_segunda_tarea(self):
        await SfcSalesforceMapper.iniciar_refresco_periodico()
        primera_tarea = SfcSalesforceMapper._refresh_task

        await SfcSalesforceMapper.iniciar_refresco_periodico()

        self.assertIs(SfcSalesforceMapper._refresh_task, primera_tarea)

    async def test_si_la_tarea_anterior_ya_termino_arranca_una_nueva(self):
        tarea_terminada = asyncio.create_task(asyncio.sleep(0))
        await tarea_terminada
        SfcSalesforceMapper._refresh_task = tarea_terminada

        await SfcSalesforceMapper.iniciar_refresco_periodico()

        self.assertIsNot(SfcSalesforceMapper._refresh_task, tarea_terminada)
        self.assertFalse(SfcSalesforceMapper._refresh_task.done())


class TestLoopRefrescoPeriodico(unittest.IsolatedAsyncioTestCase):
    """Prueba el cuerpo del loop directamente (sin pasar por asyncio.sleep real)
    para no depender de CACHE_TTL_SEGUNDOS (600s) en el tiempo del test."""

    async def test_espera_el_ttl_y_luego_refresca(self):
        with patch("app.core.mapping.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch.object(SfcSalesforceMapper, "obtener_catalogos_y_mapeos", new_callable=AsyncMock) as mock_refresh:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            with self.assertRaises(asyncio.CancelledError):
                await SfcSalesforceMapper._loop_refresco_periodico()

        mock_sleep.assert_any_call(SfcSalesforceMapper.CACHE_TTL_SEGUNDOS)
        mock_refresh.assert_awaited_once_with(http_client=None)

    async def test_propaga_el_http_client_recibido(self):
        cliente_falso = object()
        with patch("app.core.mapping.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch.object(SfcSalesforceMapper, "obtener_catalogos_y_mapeos", new_callable=AsyncMock) as mock_refresh:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            with self.assertRaises(asyncio.CancelledError):
                await SfcSalesforceMapper._loop_refresco_periodico(http_client=cliente_falso)

        mock_refresh.assert_awaited_once_with(http_client=cliente_falso)

    async def test_un_error_en_el_refresco_no_mata_el_loop(self):
        """Un fallo (ej. Google Sheets caído) no debe tumbar la tarea de fondo --
        el siguiente ciclo debe seguir intentando."""
        with patch("app.core.mapping.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch.object(
                 SfcSalesforceMapper, "obtener_catalogos_y_mapeos", new_callable=AsyncMock,
                 side_effect=[RuntimeError("google sheets caído"), None]
             ) as mock_refresh:
            mock_sleep.side_effect = [None, None, asyncio.CancelledError()]
            with self.assertRaises(asyncio.CancelledError):
                await SfcSalesforceMapper._loop_refresco_periodico()

        self.assertEqual(mock_refresh.await_count, 2)


class TestDetenerRefrescoPeriodico(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._orig_task = SfcSalesforceMapper._refresh_task
        SfcSalesforceMapper._refresh_task = None

    async def asyncTearDown(self):
        SfcSalesforceMapper._refresh_task = self._orig_task

    async def test_sin_tarea_activa_no_lanza(self):
        await SfcSalesforceMapper.detener_refresco_periodico()  # No debe lanzar.
        self.assertIsNone(SfcSalesforceMapper._refresh_task)

    async def test_cancela_la_tarea_y_limpia_el_estado(self):
        await SfcSalesforceMapper.iniciar_refresco_periodico()
        tarea = SfcSalesforceMapper._refresh_task

        await SfcSalesforceMapper.detener_refresco_periodico()

        self.assertTrue(tarea.cancelled() or tarea.done())
        self.assertIsNone(SfcSalesforceMapper._refresh_task)


if __name__ == "__main__":
    unittest.main()
