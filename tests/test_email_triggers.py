# tests/test_email_triggers.py
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from app.services.queue_service import QueueService
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.services.email_service import EmailAlertService
from app.workers.scheduler import reintentar_despachos_pendientes_job
from app.core.exceptions import SfcIntegrationException


class TestEmailTriggers(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.db_mock = AsyncMock()
        self.queue_service = QueueService(db_session=self.db_mock)

    @patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock)
    @patch.object(EmailAlertService, "notificar_umbral_cola", new_callable=AsyncMock)
    async def test_encolar_despacho_dispara_alertas(self, mock_umbral, mock_falla):
        """Valida que al encolar un caso se dispare la alerta de infraestructura y el umbral si corresponde."""
        self.queue_service.contar_pendientes = AsyncMock(return_value=100)
        self.db_mock.refresh = AsyncMock()

        smart_code = "142316551509974606"
        error_msg = "HTTP 502 Bad Gateway"

        await self.queue_service.encolar_despacho(
            smart_code=smart_code,
            tipo_operacion="CREACION",
            payload_json={"Smart_Code__c": smart_code},
            error_inicial=error_msg
        )

        mock_falla.assert_called_once_with(
            smart_code=smart_code,
            error_msg=error_msg
        )
        mock_umbral.assert_called_once_with(total_pendientes=100)

    @patch.object(EmailAlertService, "notificar_error_no_mapeado", new_callable=AsyncMock)
    async def test_orquestador_dispara_correo_error_no_mapeado(self, mock_no_mapeado):
        """Valida que un error no reconocido en la SFC notifique exclusivamente al desarrollador."""
        sfc_mock = MagicMock()
        
        exc = SfcIntegrationException(
            "Error desconocido desde la SFC",
            "UNKNOWN_ERROR",
            None,
            "Error desconocido desde la SFC",
            "Revisar payload"
        )
        exc.status_code = 400
        exc.is_unmapped = True

        m2_mock = AsyncMock()
        m2_mock.ejecutar_envio_momento_2.side_effect = exc

        orquestador = DespachoQuejaOrquestador(sfc_client=sfc_mock)
        orquestador.m2_service = m2_mock

        payload_mock = MagicMock()
        payload_mock.Smart_Code__c = "142399988877"
        payload_mock.Status = "New"
        payload_mock.ClosedDate = None
        payload_mock.Favorabilidad__c = None
        payload_mock.tipo_fraude__c = None
        payload_mock.modalidad_fraude__c = None

        with self.assertRaises(SfcIntegrationException):
            await orquestador.procesar_despacho(payload=payload_mock)

        mock_no_mapeado.assert_called_once_with(
            status_code=400,
            raw_message="Error desconocido desde la SFC",
            sfc_field=None,
            smart_code="142399988877"
        )

    async def test_scheduler_dispara_digest_sla_y_recuperacion(self):
        """Valida las alertas de digest por envejecimiento (>12h) y autorrecuperación en el job de scheduler."""
        casos_vencidos = [{
            "smart_code": "1423111",
            "fecha_encolado": "2026-07-22 10:00:00",
            "horas_en_cola": 14.5,
            "reintentos": 3,
            "ultimo_error": "Timeout"
        }]

        reg = MagicMock()
        reg.id = 1
        reg.smart_code = "1423111"
        reg.payload_json = {"Smart_Code__c": "1423111"}

        # 🎯 Asignación explícita mediante context manager (sin ambigüedades de orden de parámetros)
        with patch.object(EmailAlertService, "notificar_casos_vencimiento_sla", new_callable=AsyncMock) as mock_sla, \
             patch.object(EmailAlertService, "notificar_recuperacion_sfc", new_callable=AsyncMock) as mock_recuperacion, \
             patch("app.workers.scheduler.AsyncSessionLocal") as mock_session_local, \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            session_mock = AsyncMock()
            mock_session_local.return_value.__aenter__.return_value = session_mock

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=casos_vencidos)
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg])
            instance_qs.contar_pendientes = AsyncMock(return_value=0)
            instance_qs.marcar_exitoso = AsyncMock()

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            await reintentar_despachos_pendientes_job()

            # Verificaciones
            mock_sla.assert_called_once_with(casos_vencidos=casos_vencidos)
            mock_recuperacion.assert_called_once_with(total_despachados=1)


if __name__ == "__main__":
    unittest.main()