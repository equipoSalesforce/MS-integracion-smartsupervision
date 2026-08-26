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
from app.core.exceptions import SfcIntegrationException
from app.workers.scheduler import reintentar_despachos_pendientes_job, _es_falla_infraestructura


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
        reg.payload_hash = "hash-de-prueba"
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

    async def test_sfc_completado_sin_sfc_response_no_lanza_attributeerror(self):
        """
        🔴 FIX N5 (revisión externa v5, 2026-08-25): MARK_SFC_DONE_LUA_SCRIPT sólo
        escribe sfc_response si hubo un valor -- un item con sfc_completado=True y
        sfc_response=None es representable. Sin el "or {}" de respaldo,
        _ejecutar_paso_notificacion_crm hace resultado_sfc.get(...) sobre None y
        lanza AttributeError, que el except genérico interpreta como fallo real
        (consume intento) en vez de completar el reintento del webhook con normalidad.
        """
        reg = self._item_base(sfc_completado=True, sfc_response=None)
        redis_mock = AsyncMock()

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.workers.scheduler.get_redis_client", return_value=redis_mock), \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch(
                 "app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia",
                 new_callable=AsyncMock,
                 return_value=(False, "Fallo de red/comunicación al notificar al CRM: 503 Service Unavailable")
             ), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg])
            instance_qs.contar_pendientes = AsyncMock(return_value=1)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=30.0)
            instance_qs.registrar_fallo = AsyncMock(return_value="failed")
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            instance_qs.marcar_sfc_completado = AsyncMock()
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(return_value=reg)

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            await reintentar_despachos_pendientes_job()

            # El fallo debe venir de que el webhook falló por infraestructura
            # (consumir_intento=False) -- NO de un AttributeError genérico
            # (que hubiera hecho que registrar_fallo se llame sin ese kwarg explícito
            # y sin distinguir infraestructura de negocio).
            instance_qs.registrar_fallo.assert_called_once_with(
                item=reg, error_msg=ANY, worker_id=ANY, consumir_intento=False
            )
            error_msg_usado = instance_qs.registrar_fallo.call_args.kwargs["error_msg"]
            self.assertNotIn("AttributeError", error_msg_usado)

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
             patch("app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia", new_callable=AsyncMock, return_value=(True, None)) as mock_webhook, \
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
             patch("app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia", new_callable=AsyncMock, return_value=(True, None)), \
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
                sfc_response=resultado_sfc,
                payload_hash=reg.payload_hash
            )

    async def test_webhook_falla_por_infraestructura_no_consume_intento(self):
        """
        Si el webhook al CRM falla por una caída de infraestructura (5xx/timeout/
        red -- detectable vía _es_falla_infraestructura sobre el detalle que ahora
        retorna CrmWebhookService), registrar_fallo debe llamarse con
        consumir_intento=False: la SFC ya proceso el caso con éxito, así que agotar
        el límite de reintentos por una caída puramente del CRM sería un falso
        FAILED_FINAL/DLQ.
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
             patch(
                 "app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia",
                 new_callable=AsyncMock,
                 return_value=(False, "Fallo de red/comunicación al notificar al CRM: 503 Service Unavailable")
             ), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg])
            instance_qs.contar_pendientes = AsyncMock(return_value=1)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=30.0)
            instance_qs.registrar_fallo = AsyncMock(return_value="failed")
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            instance_qs.marcar_sfc_completado = AsyncMock()
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(return_value=reg)

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            await reintentar_despachos_pendientes_job()

            instance_qs.registrar_fallo.assert_called_once_with(
                item=reg, error_msg=ANY, worker_id=ANY, consumir_intento=False
            )

    async def test_webhook_infraestructura_estancado_mas_del_umbral_escala_a_error(self):
        """
        🟡 FIX (hallazgo de revisión externa, 2026-08-25, §12): un reintento infinito
        de webhook (no consume intentos, nunca llega a FAILED_FINAL) debe distinguirse
        en logs de un fallo normal una vez supera el umbral -- para dar visibilidad
        temprana y específica (la causa es el webhook, no la SFC) sin esperar a la
        alerta de SLA genérica de 12h.
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        creado_hace_3_horas = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(hours=3)).isoformat()
        reg = self._item_base(
            sfc_completado=True,
            sfc_response={"status": "success", "message": "Ya procesado en SFC"},
            created_at=creado_hace_3_horas
        )
        redis_mock = AsyncMock()

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.workers.scheduler.get_redis_client", return_value=redis_mock), \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch(
                 "app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia",
                 new_callable=AsyncMock,
                 return_value=(False, "Fallo de red/comunicación al notificar al CRM: 503 Service Unavailable")
             ), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg])
            instance_qs.contar_pendientes = AsyncMock(return_value=1)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=30.0)
            instance_qs.registrar_fallo = AsyncMock(return_value="failed")
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            instance_qs.marcar_sfc_completado = AsyncMock()
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(return_value=reg)

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            with self.assertLogs("app.workers.scheduler", level="ERROR") as logs:
                await reintentar_despachos_pendientes_job()

            self.assertTrue(any("REINTENTO_INFINITO_WEBHOOK" in m for m in logs.output))

    async def test_webhook_infraestructura_reciente_no_escala_a_error(self):
        """Contraprueba: si el ítem lleva poco tiempo (bajo el umbral), sigue como un
        warning normal -- no debe dispararse la escalación todavía."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        creado_hace_10_min = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(minutes=10)).isoformat()
        reg = self._item_base(
            sfc_completado=True,
            sfc_response={"status": "success", "message": "Ya procesado en SFC"},
            created_at=creado_hace_10_min
        )
        redis_mock = AsyncMock()

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.workers.scheduler.get_redis_client", return_value=redis_mock), \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch(
                 "app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia",
                 new_callable=AsyncMock,
                 return_value=(False, "Fallo de red/comunicación al notificar al CRM: 503 Service Unavailable")
             ), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg])
            instance_qs.contar_pendientes = AsyncMock(return_value=1)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=30.0)
            instance_qs.registrar_fallo = AsyncMock(return_value="failed")
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            instance_qs.marcar_sfc_completado = AsyncMock()
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(return_value=reg)

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            with self.assertLogs("app.workers.scheduler", level="WARNING") as logs:
                await reintentar_despachos_pendientes_job()

            self.assertFalse(any("REINTENTO_INFINITO_WEBHOOK" in m for m in logs.output))

    async def test_webhook_falla_por_negocio_si_consume_intento(self):
        """
        Un rechazo del webhook por contrato de negocio (ej. success!=true, WAF,
        correlación de caso) no es una caída de infraestructura -- debe seguir
        consumiendo intento como cualquier otro fallo.
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
             patch(
                 "app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia",
                 new_callable=AsyncMock,
                 return_value=(False, "El CRM no confirmó éxito explícito (success=False).")
             ), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg])
            instance_qs.contar_pendientes = AsyncMock(return_value=1)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=30.0)
            instance_qs.registrar_fallo = AsyncMock(return_value="failed")
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            instance_qs.marcar_sfc_completado = AsyncMock()
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(return_value=reg)

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            await reintentar_despachos_pendientes_job()

            instance_qs.registrar_fallo.assert_called_once_with(
                item=reg, error_msg=ANY, worker_id=ANY, consumir_intento=True
            )


class TestEsFallaInfraestructuraUsaExcepcionEstructurada(unittest.TestCase):
    """
    🔴 FIX (hallazgo de revisión externa, 2026-08-25): antes _es_falla_infraestructura
    clasificaba SIEMPRE por subcadena sobre el mensaje libre de error -- para una
    SfcIntegrationException (el caso más común de fallo real al despachar), ese texto
    es el mensaje devuelto por la SFC, que puede contener un monto/código de caso que
    coincida por casualidad con "503"/"429"/etc, clasificando mal un rechazo de negocio
    real como caída transitoria de infraestructura. Ahora usa
    SfcIntegrationException.es_transitoria (status_code/error_type estructurados) en
    vez del texto, cuando hay una excepción disponible.
    """

    def _exc(self, status_code, error_type, raw_message):
        return SfcIntegrationException(
            status_code=status_code, error_type=error_type, sfc_field=None,
            raw_message=raw_message, crm_action="Corrija el dato."
        )

    def test_rechazo_de_negocio_con_503_en_el_mensaje_no_se_clasifica_como_infraestructura(self):
        """El escenario exacto del hallazgo: status_code=400 (rechazo real de negocio),
        pero el mensaje de la SFC menciona '503' como parte de un monto/código -- antes
        esto daba un falso positivo por subcadena."""
        exc = self._exc(400, "CRM_PAYLOAD_VALIDATION_ERROR", "El monto_reclamado (503829.50) supera el límite permitido")
        self.assertFalse(_es_falla_infraestructura(str(exc), exc=exc))

    def test_caida_real_5xx_si_se_clasifica_como_infraestructura(self):
        exc = self._exc(503, "SFC_DOWN", "Servicio no disponible temporalmente")
        self.assertTrue(_es_falla_infraestructura(str(exc), exc=exc))

    def test_error_type_transitorio_con_status_code_no_5xx_igual_se_clasifica_como_infraestructura(self):
        exc = self._exc(429, "THROTTLED_ERROR", "Cuota excedida")
        self.assertTrue(_es_falla_infraestructura(str(exc), exc=exc))

    def test_sin_excepcion_estructurada_cae_al_match_por_subcadena_como_antes(self):
        """Excepciones genéricas (ConnectionError, httpx.TimeoutException, etc.) o el
        mensaje del webhook al CRM no tienen campos estructurados -- se mantiene el
        comportamiento anterior como fallback."""
        self.assertTrue(_es_falla_infraestructura("Connection timeout al conectar con el host"))
        self.assertFalse(_es_falla_infraestructura("El CRM no confirmó éxito explícito (success=False)."))


class TestClasificacionEstructuradaNoDifiereElRestoDelLotePorFalsoPositivo(unittest.IsolatedAsyncioTestCase):
    """Verifica el efecto de punta a punta del fix: un rechazo de negocio real (con
    '503' coincidiendo en el texto) ya NO dispara el diferimiento del resto del lote
    del ciclo del scheduler -- sólo una caída de infraestructura genuina debe hacerlo."""

    def _item_base(self, item_id, smart_code):
        reg = MagicMock()
        reg.id = item_id
        reg.smart_code = smart_code
        reg.payload_json = {"Smart_Code__c": smart_code}
        reg.correlation_id = "N/A"
        reg.sfc_completado = False
        reg.sfc_response = {}
        reg.version = 1
        reg.payload_hash = "hash-de-prueba"
        reg.to_dict = MagicMock(return_value={"correlation_id": "N/A"})
        return reg

    async def test_rechazo_de_negocio_con_503_en_el_texto_no_difiere_el_resto_del_lote(self):
        reg_1 = self._item_base(1, "SC-A")
        reg_2 = self._item_base(2, "SC-B")
        redis_mock = AsyncMock()

        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.workers.scheduler.get_redis_client", return_value=redis_mock), \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch("app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia", new_callable=AsyncMock, return_value=(True, None)), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[reg_1, reg_2])
            instance_qs.contar_pendientes = AsyncMock(return_value=1)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=30.0)
            instance_qs.registrar_fallo = AsyncMock(return_value="failed")
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            instance_qs.marcar_sfc_completado = AsyncMock(return_value="completed")
            instance_qs.diferir_pendientes_por_caida_sfc = AsyncMock()
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(side_effect=[reg_1, reg_2])

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(
                side_effect=[
                    SfcIntegrationException(
                        status_code=400, error_type="CRM_PAYLOAD_VALIDATION_ERROR", sfc_field="monto",
                        raw_message="El monto_reclamado (503829.50) supera el límite permitido",
                        crm_action="Corrija el dato."
                    ),
                    {"status": "success", "codigo_queja": "SC-B"}
                ]
            )

            await reintentar_despachos_pendientes_job()

            instance_qs.diferir_pendientes_por_caida_sfc.assert_not_called()
            # El segundo item del lote SÍ debe procesarse -- no se cortó el ciclo.
            instance_orq.procesar_despacho_raw_json.assert_called()
            self.assertEqual(instance_orq.procesar_despacho_raw_json.await_count, 2)


class TestUmbralFallasInfraConsecutivasParaDiferir(unittest.IsolatedAsyncioTestCase):
    """
    🟡 FIX (hallazgo B3, auditoría adversarial 2026-08-25): antes, la PRIMERA falla
    de infraestructura del ciclo difería TODO el resto del lote 5 minutos y cortaba
    -- con la SFC intermitente, cada ciclo procesaba un solo caso. Ahora se exigen
    UMBRAL_FALLAS_INFRA_CONSECUTIVAS_PARA_DIFERIR (3) fallas de infraestructura
    seguidas antes de asumir que la SFC está caída y diferir el resto.
    """

    def _item_base(self, item_id, smart_code):
        reg = MagicMock()
        reg.id = item_id
        reg.smart_code = smart_code
        reg.payload_json = {"Smart_Code__c": smart_code}
        reg.correlation_id = "N/A"
        reg.sfc_completado = False
        reg.sfc_response = {}
        reg.version = 1
        reg.payload_hash = "hash-de-prueba"
        reg.to_dict = MagicMock(return_value={"correlation_id": "N/A"})
        return reg

    def _exc_infra(self):
        return SfcIntegrationException(
            status_code=500, error_type="INFRASTRUCTURE_ERROR", sfc_field=None,
            raw_message="Internal Server Error", crm_action="Reintente la operación más tarde."
        )

    async def _ejecutar_ciclo(self, items, side_effects):
        redis_mock = AsyncMock()
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.workers.scheduler.get_redis_client", return_value=redis_mock), \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch(
                 "app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia",
                 new_callable=AsyncMock, return_value=(True, None)
             ), \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=items)
            instance_qs.contar_pendientes = AsyncMock(return_value=0)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=30.0)
            instance_qs.registrar_fallo = AsyncMock(return_value="failed")
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            instance_qs.marcar_sfc_completado = AsyncMock(return_value="completed")
            instance_qs.diferir_pendientes_por_caida_sfc = AsyncMock()
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(side_effect=list(items))

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(side_effect=side_effects)

            await reintentar_despachos_pendientes_job()

            return instance_qs, instance_orq

    async def test_dos_fallas_consecutivas_bajo_el_umbral_no_difiere_y_sigue_el_lote(self):
        items = [self._item_base(i, f"SC-{i}") for i in range(1, 6)]
        side_effects = [
            self._exc_infra(), self._exc_infra(),
            {"status": "success"}, {"status": "success"}, {"status": "success"}
        ]

        instance_qs, instance_orq = await self._ejecutar_ciclo(items, side_effects)

        instance_qs.diferir_pendientes_por_caida_sfc.assert_not_called()
        self.assertEqual(instance_orq.procesar_despacho_raw_json.await_count, 5)

    async def test_tres_fallas_consecutivas_alcanza_el_umbral_y_difiere_el_resto(self):
        items = [self._item_base(i, f"SC-{i}") for i in range(1, 6)]
        side_effects = [
            self._exc_infra(), self._exc_infra(), self._exc_infra(),
            {"status": "success"}, {"status": "success"}
        ]

        instance_qs, instance_orq = await self._ejecutar_ciclo(items, side_effects)

        instance_qs.diferir_pendientes_por_caida_sfc.assert_called_once()
        ids_diferidos = instance_qs.diferir_pendientes_por_caida_sfc.call_args.kwargs["registro_ids"]
        self.assertEqual(ids_diferidos, [4, 5])
        # El ciclo se cortó tras la tercera falla -- los items 4 y 5 no se intentaron.
        self.assertEqual(instance_orq.procesar_despacho_raw_json.await_count, 3)

    async def test_exito_intermedio_resetea_el_contador_de_fallas_consecutivas(self):
        items = [self._item_base(i, f"SC-{i}") for i in range(1, 6)]
        side_effects = [
            self._exc_infra(), self._exc_infra(),
            {"status": "success"},
            self._exc_infra(), self._exc_infra()
        ]

        instance_qs, instance_orq = await self._ejecutar_ciclo(items, side_effects)

        # Nunca se acumulan 3 fallas SEGUIDAS (el éxito de en medio resetea el contador).
        instance_qs.diferir_pendientes_por_caida_sfc.assert_not_called()
        self.assertEqual(instance_orq.procesar_despacho_raw_json.await_count, 5)


class TestLimpiarCheckpointTrasPersistenciaDurable(unittest.IsolatedAsyncioTestCase):
    """
    🔴 FIX (hallazgo N2, revisión externa v5, 2026-08-25): el checkpoint de adjuntos
    se limpiaba apenas la SFC confirmaba éxito, ANTES de que marcar_sfc_completado
    persistiera ese hecho de forma durable -- si esa persistencia fallaba, el
    próximo ciclo reejecutaba Momento 3 completo con el checkpoint ya vacío,
    retransmitiendo TODOS los adjuntos a la SFC por segunda vez. Ahora el worker
    limpia por su cuenta, sólo después de confirmar que SFC_DONE quedó persistido
    (limpiar_checkpoint_en_exito=False evita que el orquestador limpie antes).
    """

    def _item_base(self, payload_json: dict) -> MagicMock:
        reg = MagicMock()
        reg.id = 1
        reg.smart_code = "SC-CHK"
        reg.payload_json = payload_json
        reg.correlation_id = "N/A"
        reg.sfc_completado = False
        reg.sfc_response = {}
        reg.version = 1
        reg.payload_hash = "hash-de-prueba"
        reg.to_dict = MagicMock(return_value={"correlation_id": "N/A"})
        return reg

    async def _ejecutar_ciclo(self, item, marcar_sfc_completado_return=None, marcar_sfc_completado_side_effect=None):
        redis_mock = AsyncMock()
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.workers.scheduler.get_redis_client", return_value=redis_mock), \
             patch("app.workers.scheduler.get_sfc_client"), \
             patch("app.workers.scheduler.get_s3_client"), \
             patch(
                 "app.workers.scheduler.CrmWebhookService.notificar_resolucion_contingencia",
                 new_callable=AsyncMock, return_value=(True, None)
             ), \
             patch("app.services.despacho_queja_orchestrator.IdempotencyService") as MockIdempotencyService, \
             patch("app.workers.scheduler.QueueService") as MockQueueService, \
             patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrquestador:

            instancia_idempotencia = MockIdempotencyService.return_value
            instancia_idempotencia.limpiar_checkpoint_archivos = AsyncMock()

            instance_qs = MockQueueService.return_value
            instance_qs.obtener_casos_vencidos_sla = AsyncMock(return_value=[])
            instance_qs.obtener_pendientes_para_reintento = AsyncMock(return_value=[item])
            instance_qs.contar_pendientes = AsyncMock(return_value=0)
            instance_qs.obtener_edad_item_mas_antiguo_pendiente = AsyncMock(return_value=None)
            instance_qs.registrar_fallo = AsyncMock(return_value="failed")
            instance_qs.marcar_exitoso = AsyncMock(return_value="completed")
            if marcar_sfc_completado_side_effect is not None:
                instance_qs.marcar_sfc_completado = AsyncMock(side_effect=marcar_sfc_completado_side_effect)
            else:
                instance_qs.marcar_sfc_completado = AsyncMock(return_value=marcar_sfc_completado_return)
            instance_qs.reclamar_item_para_procesamiento = AsyncMock(return_value=item)

            instance_orq = MockOrquestador.return_value
            instance_orq.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            await reintentar_despachos_pendientes_job()

            return instance_orq, instancia_idempotencia

    async def test_llama_a_procesar_despacho_raw_json_con_limpiar_checkpoint_en_exito_false(self):
        item = self._item_base({"Smart_Code__c": "SC-CHK"})

        instance_orq, _ = await self._ejecutar_ciclo(item, marcar_sfc_completado_return="completed")

        instance_orq.procesar_despacho_raw_json.assert_awaited_once_with(
            item.payload_json, limpiar_checkpoint_en_exito=False
        )

    async def test_completed_y_cierre_limpia_el_checkpoint(self):
        item = self._item_base({"Smart_Code__c": "SC-CHK", "ClosedDate": "2026-01-01T00:00:00"})

        _, instancia_idempotencia = await self._ejecutar_ciclo(item, marcar_sfc_completado_return="completed")

        instancia_idempotencia.limpiar_checkpoint_archivos.assert_awaited_once_with("SC-CHK")

    async def test_completed_pero_no_es_cierre_no_limpia_el_checkpoint(self):
        item = self._item_base({"Smart_Code__c": "SC-CHK"})  # sin ClosedDate/Favorabilidad__c -> trámite

        _, instancia_idempotencia = await self._ejecutar_ciclo(item, marcar_sfc_completado_return="completed")

        instancia_idempotencia.limpiar_checkpoint_archivos.assert_not_awaited()

    async def test_version_mismatch_no_limpia_el_checkpoint_pese_a_ser_cierre(self):
        item = self._item_base({"Smart_Code__c": "SC-CHK", "ClosedDate": "2026-01-01T00:00:00"})

        _, instancia_idempotencia = await self._ejecutar_ciclo(item, marcar_sfc_completado_return="version_mismatch")

        instancia_idempotencia.limpiar_checkpoint_archivos.assert_not_awaited()

    async def test_persistencia_fallida_no_limpia_el_checkpoint_pese_a_ser_cierre(self):
        item = self._item_base({"Smart_Code__c": "SC-CHK", "ClosedDate": "2026-01-01T00:00:00"})

        _, instancia_idempotencia = await self._ejecutar_ciclo(
            item, marcar_sfc_completado_side_effect=RuntimeError("Redis no disponible")
        )

        instancia_idempotencia.limpiar_checkpoint_archivos.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
