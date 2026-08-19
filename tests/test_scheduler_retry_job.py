# tests/test_scheduler_retry_job.py
"""
Pruebas de la lógica de decisión del job de reintentos del scheduler
(reintentar_despachos_pendientes_job), enfocadas en dos escenarios de la
auditoría técnica del 13/08/2026 (sección 8) que no tenían cobertura directa:

  - Item 5 / escenario 4.2 (primera mitad): SFC responde 200 pero la
    persistencia durable (idempotencia + SFC_DONE, fusionadas atómicamente
    desde el Nivel 1 de la auditoría v10/P0-02 -- ver queue_service.
    marcar_sfc_completado) falla. El item NO debe declararse completado ni
    contarse como fallo de reintentos (eso reenviaría a la SFC en el próximo
    ciclo, duplicando el envío) — debe alertarse como falla crítica de
    infraestructura y dejarse intacto para reintentar SÓLO la persistencia.
  - Item 6 / escenario 4.2 (segunda mitad): si el item YA tiene
    sfc_completado=True (SFC ya procesó el caso en un ciclo anterior), un
    reintento NO debe volver a llamar a la SFC — sólo debe reintentar el
    callback al CRM.
"""
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from app.services.email_service import EmailAlertService
from app.core.config import settings
from app.workers.scheduler import reintentar_despachos_pendientes_job


class TestSchedulerRetryJobDecisions(unittest.IsolatedAsyncioTestCase):

    def _item_base(self, **overrides) -> MagicMock:
        reg = MagicMock()
        reg.id = 1
        reg.smart_code = "SC-500"
        reg.payload_json = {"Smart_Code__c": "SC-500", "Case_id": "SC-500"}
        reg.correlation_id = "N/A"
        reg.sfc_completado = False
        reg.sfc_response = {}
        reg.version = 1
        reg.to_dict = MagicMock(return_value={"correlation_id": "N/A"})
        for k, v in overrides.items():
            setattr(reg, k, v)
        return reg

    async def test_sfc_200_pero_persistencia_falla_no_marca_completado_ni_cuenta_como_fallo(self):
        """
        Auditoría item 5 / escenario 4.2: SFC 200 + falla al persistir SFC_DONE.
        No debe llamarse registrar_fallo (no es un fallo de despacho, es de
        persistencia), no debe llamarse marcar_exitoso, y debe alertarse crítico.
        """
        reg = self._item_base()
        redis_mock = AsyncMock()

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alerta, \
             patch("app.workers.scheduler.get_redis_client", return_value=redis_mock), \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg])
            instance_qs.contar_pendientes = AsyncMock(return_value=1)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=30.0)
            instance_qs.registrar_fallo = AsyncMock()
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            # Nivel 1 (P0-02): registrar_exito() e idempotencia ya no existen como
            # llamada separada -- marcar_sfc_completado es ahora la única escritura
            # post-SFC (fusiona SFC_DONE + idempotencia en un solo script Lua), así
            # que es ella la que debe fallar para reproducir este escenario.
            instance_qs.marcar_sfc_completado = AsyncMock(side_effect=RuntimeError("Redis no disponible al persistir"))
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(return_value=reg)

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success", "message": "OK"})

            await reintentar_despachos_pendientes_job()

            instance_qs.marcar_sfc_completado.assert_called_once()
            instance_qs.registrar_fallo.assert_not_called()
            instance_qs.marcar_exitoso.assert_not_called()
            mock_alerta.assert_called_once()

    async def test_sfc_ya_completado_no_vuelve_a_llamar_sfc_solo_reintenta_callback(self):
        """
        Auditoría item 6 / escenario 4.2: si sfc_completado ya es True (persistido
        en un ciclo anterior), el retry no debe volver a invocar
        procesar_despacho_raw_json — sólo debe reintentar la notificación al CRM.
        """
        reg = self._item_base(
            sfc_completado=True,
            sfc_response={"status": "success", "message": "Ya procesado en SFC"}
        )
        redis_mock = AsyncMock()

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.workers.scheduler.get_redis_client", return_value=redis_mock), \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch("app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia", new_callable=AsyncMock, return_value=True) as mock_webhook, \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg])
            instance_qs.contar_pendientes = AsyncMock(return_value=0)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=None)
            instance_qs.registrar_fallo = AsyncMock()
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            instance_qs.marcar_sfc_completado = AsyncMock()
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(return_value=reg)

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            await reintentar_despachos_pendientes_job()

            instance_orq.procesar_despacho_raw_json.assert_not_called()
            instance_qs.marcar_sfc_completado.assert_not_called()
            mock_webhook.assert_called_once()
            instance_qs.marcar_exitoso.assert_called_once_with(reg.id, worker_id=ANY, expected_version=reg.version)
            instance_qs.registrar_fallo.assert_not_called()

    async def test_sfc_200_llama_marcar_sfc_completado_con_smart_code_y_payload(self):
        """
        Nivel 1 (auditoría adversarial v10, P0-02): tras SFC 200, scheduler.py debe
        llamar a marcar_sfc_completado (única escritura post-SFC, ya no una llamada
        separada a idempotency_service.registrar_exito) pasando smart_code y
        payload_dict del item -- son los datos que marcar_sfc_completado necesita
        para construir la clave/registro del Idempotency Store dentro del mismo
        script Lua atómico.
        """
        reg = self._item_base()
        redis_mock = AsyncMock()
        resultado_sfc = {"status": "success", "codigo_queja": "SC-500"}

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.workers.scheduler.get_redis_client", return_value=redis_mock), \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch("app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia", new_callable=AsyncMock, return_value=True), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg])
            instance_qs.contar_pendientes = AsyncMock(return_value=0)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=None)
            instance_qs.registrar_fallo = AsyncMock()
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            instance_qs.marcar_sfc_completado = AsyncMock(return_value="completed")
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(return_value=reg)

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value=resultado_sfc)

            await reintentar_despachos_pendientes_job()

            instance_qs.marcar_sfc_completado.assert_called_once_with(
                reg.id,
                worker_id=ANY,
                expected_version=reg.version,
                smart_code=reg.smart_code,
                payload_dict=reg.payload_json,
                sfc_response=resultado_sfc
            )


if __name__ == "__main__":
    unittest.main()
