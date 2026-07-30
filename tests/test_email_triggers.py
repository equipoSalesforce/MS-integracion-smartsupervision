import asyncio
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.services.queue_service import QueueService
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.services.email_service import EmailAlertService
from app.workers.scheduler import reintentar_despachos_pendientes_job
from app.core.exceptions import SfcIntegrationException
from app.core.config import settings


class TestEmailTriggers(unittest.IsolatedAsyncioTestCase):

    async def test_encolar_despacho_dispara_alerta_infraestructura_cuando_cola_vacia(self):
        """Valida que si la cola está vacía (0 pendientes), se notifique la caída de infraestructura."""
        smart_code = "142316551509974606"
        error_msg = "HTTP 502 Bad Gateway"

        db_mock = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_result.scalars.return_value.one_or_none.return_value = None
        db_mock.execute.return_value = mock_result
        db_mock.add = MagicMock()
        db_mock.commit = AsyncMock()
        db_mock.refresh = AsyncMock()

        queue_service = QueueService(db_session=db_mock)
        # 🎯 Retorna 0 para simular la primera falla (cola vacía)
        queue_service.contar_pendientes = AsyncMock(return_value=0)

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_falla, \
             patch.object(EmailAlertService, "notificar_umbral_cola", new_callable=AsyncMock) as mock_umbral:

            await queue_service.encolar_despacho(
                smart_code=smart_code,
                tipo_operacion="CREACION",
                payload_json={"Smart_Code__c": smart_code},
                error_inicial=error_msg
            )
            
            await asyncio.sleep(0)

            # Debe llamarse a alerta de infraestructura y NO a la de umbral
            mock_falla.assert_called_once_with(
                smart_code=smart_code,
                error_msg=error_msg
            )
            mock_umbral.assert_not_called()

    async def test_encolar_despacho_dispara_alerta_umbral_al_llegar_a_100(self):
        """Valida que si al encolar un caso se alcanzan 100 pendientes, se notifique la alerta de umbral."""
        smart_code = "142316551509974606"
        error_msg = "HTTP 502 Bad Gateway"

        db_mock = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_result.scalars.return_value.one_or_none.return_value = None
        db_mock.execute.return_value = mock_result
        db_mock.add = MagicMock()
        db_mock.commit = AsyncMock()
        db_mock.refresh = AsyncMock()

        queue_service = QueueService(db_session=db_mock)
        # 🎯 Retorna 99 para que con el nuevo caso sume 100
        queue_service.contar_pendientes = AsyncMock(return_value=99)

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_falla, \
             patch.object(EmailAlertService, "notificar_umbral_cola", new_callable=AsyncMock) as mock_umbral:

            await queue_service.encolar_despacho(
                smart_code=smart_code,
                tipo_operacion="CREACION",
                payload_json={"Smart_Code__c": smart_code},
                error_inicial=error_msg
            )
            
            await asyncio.sleep(0)

            # NO debe llamarse a alerta de infraestructura (ya había casos), pero SÍ a la de umbral
            mock_falla.assert_not_called()
            mock_umbral.assert_called_once_with(total_pendientes=100)

    @patch.object(EmailAlertService, "notificar_error_no_mapeado", new_callable=AsyncMock)
    async def test_orquestador_dispara_correo_error_no_mapeado(self, mock_no_mapeado):
        """Valida que un error no reconocido en la SFC notifique exclusivamente al desarrollador."""
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True):
            sfc_mock = MagicMock()
            
            # 1. Crear excepción de integración no mapeada
            exc = SfcIntegrationException(
                status_code=400,
                error_type="UNKNOWN_ERROR",
                sfc_field=None,
                raw_message="Error desconocido desde la SFC",
                crm_action="Revisar payload"
            )
            exc.is_unmapped = True

            # 2. Configurar AMBOS métodos de la SFC como AsyncMock con la excepción
            sfc_mock.post_nueva_queja = AsyncMock(side_effect=exc)
            sfc_mock.put_actualizar_queja = AsyncMock(side_effect=exc)
            sfc_mock.post_adjunto_queja = AsyncMock()

            # 3. Inicializar el orquestador real
            orquestador = DespachoQuejaOrquestador(sfc_client=sfc_mock, s3_client=MagicMock())

            # 4. Payload canónico completo de CRM
            smart_code = "999000111222"
            payload_dict = {
                "Case_id": smart_code,
                "CreatedDate": "2026-07-21T10:00:00",
                "Status": "New",
                "status": "New",
                "SuppliedName": "Juan Perez",
                "SC_id_type__c": "CC",
                "id_number__c": "123456789",
                "sc_genero__c": "No Aplica",
                "tipo_de_persona__c": "B2C",
                "sc_LGBTIQ__c": "No",
                "sc_Condicion_especial__c": "No aplica",
                "SuppliedPhone": "3001234567",
                "SuppliedEmail": "juan@test.com",
                "direccion__c": "Calle 123",
                "Departamento__c": "Bogotá D.C.",
                "SC_municipio__c": "Bogotá D.C.",
                "canal__c": "Internet",
                "punto_recepcion": "Manual",
                "Instancia_de_recepcion__c": "Entidad vigilada",
                "admision_col__c": "No Aplica",
                "Description": "Prueba error no mapeado",
                "smart_anexo_queja__c": False,
                "Tutela__c": "No",
                "Ente_de_control__c": "Otros",
                "smart_escalamiento_DCF__c": "No",
                "Product__c": "Cuenta perfil",
                "smart_Producto_nombre__c": "Ahorro",
                "Categorias_COL__c": "Transacción no reconocida",
                "archivos_s3": []
            }

            payload_obj = QuejaUnificadaCrmInput.model_validate(payload_dict)

            # 5. Ejecutar y verificar que se re-lance la excepción y se dispare el correo
            with self.assertRaises(SfcIntegrationException):
                await orquestador.procesar_despacho(payload=payload_obj)

            mock_no_mapeado.assert_called_once_with(
                status_code=400,
                raw_message="[UNKNOWN_ERROR] Error desconocido desde la SFC",
                sfc_field=None,
                smart_code=f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}{smart_code}"
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

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "notificar_casos_vencimiento_sla", new_callable=AsyncMock) as mock_sla, \
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

            mock_sla.assert_called_once_with(casos_vencidos=casos_vencidos)
            mock_recuperacion.assert_called_once_with(total_despachados=1)


if __name__ == "__main__":
    unittest.main()