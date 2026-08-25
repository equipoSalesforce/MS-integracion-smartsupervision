# tests/test_routes_quejas_despacho.py
"""
Cobertura de ramas de app/api/routes_quejas.py no cubiertas por
test_integration_momento_2.py / test_error_responses_consistency.py:
los tres casos de _construir_respuesta_idempotente, el camino de
"ya encolado"/doble-falla de _encolar_despacho_por_contingencia, el 400
por error de orquestación, la falla (no crítica para la respuesta) al
persistir el registro de éxito, y el endpoint GET /queue.

Los dos helpers privados (_construir_respuesta_idempotente,
_encolar_despacho_por_contingencia) se prueban directo, sin pasar por
HTTP, para no tener que simular los scripts Lua de idempotencia sólo
para llegar a una rama de formateo de respuesta.
"""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes_quejas import _construir_respuesta_idempotente, _encolar_despacho_por_contingencia
from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.main import app
from app.core.config import settings
from app.api.dependencies import get_sfc_client, get_s3_client


def _payload_valido() -> QuejaUnificadaCrmInput:
    fecha_reciente = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
    return QuejaUnificadaCrmInput(
        Smart_Code__c="16551509974606",
        CreatedDate=fecha_reciente,
        SuppliedName="Camila Salas",
        SC_id_type__c="CC",
        id_number__c="1040011014",
        tipo_de_persona__c="B2C",
        direccion__c="Calle 93 # 11-11",
        punto_recepcion="Manual",
        Description="Prueba de queja",
        smart_anexo_queja__c=False,
        Ente_de_control__c="Otros",
        Product__c="Cuenta perfil",
        Categorias_COL__c="Transacción no reconocida",
        smart_escalamiento_DCF__c="No",
        archivos_s3=[],
    )


class TestConstruirRespuestaIdempotente(unittest.TestCase):

    def test_redis_no_disponible_retorna_503(self):
        response = _construir_respuesta_idempotente({"status": "redis_unavailable", "message": "caido"})
        self.assertEqual(response.status_code, 503)

    def test_hit_exitoso_previo_retorna_200_con_header(self):
        response = _construir_respuesta_idempotente({"status": "success", "sfc_response": {"a": 1}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("x-idempotent-hit"), "true")

    def test_operacion_en_proceso_o_encolada_retorna_202(self):
        response = _construir_respuesta_idempotente({"status": "queued", "smart_code": "X"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers.get("x-idempotent-hit"), "true")


class TestEncolarDespachoPorContingencia(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.idempotency_service_mock = MagicMock()
        self.idempotency_service_mock.registrar_encolado = AsyncMock()

    async def test_encolado_normal_retorna_202_queued(self):
        item_mock = MagicMock(id=1, es_duplicado=False)
        with patch("app.api.routes_quejas.QueueService") as mock_queue_cls:
            mock_queue_cls.return_value.encolar_despacho = AsyncMock(return_value=item_mock)
            response, exitoso = await _encolar_despacho_por_contingencia(
                _payload_valido(), {"a": 1}, self.idempotency_service_mock, "SFC caida", "timeout"
            )
        self.assertTrue(exitoso)
        self.assertEqual(response.status_code, 202)

    async def test_ya_encolado_retorna_202_already_queued(self):
        item_mock = MagicMock(id=1, es_duplicado=True)
        with patch("app.api.routes_quejas.QueueService") as mock_queue_cls:
            mock_queue_cls.return_value.encolar_despacho = AsyncMock(return_value=item_mock)
            response, exitoso = await _encolar_despacho_por_contingencia(
                _payload_valido(), {"a": 1}, self.idempotency_service_mock, "SFC caida", "timeout"
            )
        self.assertTrue(exitoso)
        self.assertIn(b"already_queued", response.body)

    async def test_fallo_doble_sfc_y_redis_retorna_503_y_alerta(self):
        with patch("app.api.routes_quejas.QueueService") as mock_queue_cls, \
             patch("app.api.routes_quejas.EmailAlertService.notificar_falla_infraestructura", new=AsyncMock()) as mock_alerta:
            mock_queue_cls.return_value.encolar_despacho = AsyncMock(side_effect=ConnectionError("redis caido"))
            response, exitoso = await _encolar_despacho_por_contingencia(
                _payload_valido(), {"a": 1}, self.idempotency_service_mock, "SFC caida", "timeout"
            )
        self.assertFalse(exitoso)
        self.assertEqual(response.status_code, 503)
        mock_alerta.assert_awaited_once()

    async def test_fallo_doble_no_registra_encolado_en_idempotencia(self):
        """Si encolar_despacho falla (fallo doble SFC+Redis), no debe intentarse
        registrar_encolado sobre un item que nunca llegó a existir en la cola."""
        with patch("app.api.routes_quejas.QueueService") as mock_queue_cls, \
             patch("app.api.routes_quejas.EmailAlertService.notificar_falla_infraestructura", new=AsyncMock()):
            mock_queue_cls.return_value.encolar_despacho = AsyncMock(side_effect=ConnectionError("redis caido"))
            await _encolar_despacho_por_contingencia(
                _payload_valido(), {"a": 1}, self.idempotency_service_mock, "SFC caida", "timeout"
            )
        self.idempotency_service_mock.registrar_encolado.assert_not_awaited()

    async def test_payload_json_de_la_cola_usa_el_snapshot_pre_mutacion_no_el_del_payload_mutado(self):
        """
        🔴 FIX (hallazgo de revisión externa, 2026-08-25): regresión directa del bug --
        antes, `encolar_despacho()` recibía un `payload.model_dump()` recalculado AQUÍ,
        es decir DESPUÉS de que el orquestador mutara `payload.archivos_s3` al resolver
        `directorio_s3`. Un reintento genuino del CRM siempre calcula su propio
        raw_payload ANTES de esa mutación (nunca trae archivos_s3 resuelto, porque es un
        efecto interno de este servicio) -- así que `_item_de_cola_sigue_vigente`
        (que compara el hash del payload_json GUARDADO en la cola contra el hash del
        reintento entrante) nunca coincidía para ningún caso con directorio_s3, tratando
        el registro QUEUED como huérfano siempre y anulando la barrera anti-duplicado de
        P0-12 para ese subconjunto de casos.

        Se simula la mutación directamente sobre el objeto `payload` (equivalente a lo
        que hace DespachoQuejaOrquestador.procesar_despacho al resolver directorio_s3) Y
        se pasa un `raw_payload` distinto (el snapshot ANTERIOR a esa mutación, como lo
        calcula el caller real) -- encolar_despacho debe recibir ese `raw_payload`, no el
        estado mutado de `payload`.
        """
        payload = _payload_valido()
        raw_payload_pre_mutacion = payload.model_dump(by_alias=True, mode="json")
        self.assertEqual(raw_payload_pre_mutacion["archivos_s3"], [])

        # Mutación equivalente a resolver directorio_s3 (ocurre DESPUÉS de capturar raw_payload).
        payload.archivos_s3 = [
            {"nombre_archivo": "soporte.pdf", "s3_key": "caso/16551509974606/soporte.pdf", "bucket": "b"}
        ]
        item_mock = MagicMock(id=42, es_duplicado=False)

        with patch("app.api.routes_quejas.QueueService") as mock_queue_cls:
            mock_queue_cls.return_value.encolar_despacho = AsyncMock(return_value=item_mock)
            await _encolar_despacho_por_contingencia(
                payload, raw_payload_pre_mutacion, self.idempotency_service_mock, "SFC caida", "timeout"
            )

        payload_json_a_la_cola = mock_queue_cls.return_value.encolar_despacho.call_args.kwargs["payload_json"]
        self.assertEqual(payload_json_a_la_cola, raw_payload_pre_mutacion)
        self.assertEqual(payload_json_a_la_cola["archivos_s3"], [], "No debe reflejar el archivos_s3 ya resuelto")

    async def test_payload_de_la_cola_y_de_idempotencia_son_el_mismo_snapshot(self):
        """Ambas escrituras deben compartir exactamente el mismo dict -- por construcción,
        no por coincidencia -- para que _item_de_cola_sigue_vigente siempre las encuentre
        consistentes entre sí."""
        item_mock = MagicMock(id=42, es_duplicado=False)
        raw_payload = {"a": 1, "archivos_s3": []}

        with patch("app.api.routes_quejas.QueueService") as mock_queue_cls:
            mock_queue_cls.return_value.encolar_despacho = AsyncMock(return_value=item_mock)
            await _encolar_despacho_por_contingencia(
                _payload_valido(), raw_payload, self.idempotency_service_mock, "SFC caida", "timeout"
            )

        payload_json_a_la_cola = mock_queue_cls.return_value.encolar_despacho.call_args.kwargs["payload_json"]
        payload_dict_a_idempotencia = self.idempotency_service_mock.registrar_encolado.call_args.kwargs["payload_dict"]
        self.assertEqual(payload_json_a_la_cola, payload_dict_a_idempotencia)
        self.assertIs(payload_json_a_la_cola, raw_payload)
        self.assertIs(payload_dict_a_idempotencia, raw_payload)

    async def test_registro_encolado_incluye_registro_id_del_item_real(self):
        item_mock = MagicMock(id=77, es_duplicado=False)
        with patch("app.api.routes_quejas.QueueService") as mock_queue_cls:
            mock_queue_cls.return_value.encolar_despacho = AsyncMock(return_value=item_mock)
            await _encolar_despacho_por_contingencia(
                _payload_valido(), {"a": 1}, self.idempotency_service_mock, "SFC caida", "timeout"
            )
        self.idempotency_service_mock.registrar_encolado.assert_awaited_once()
        self.assertEqual(
            self.idempotency_service_mock.registrar_encolado.call_args.kwargs["registro_id"], 77
        )


class _RoutesQuejasHttpTestCase(unittest.TestCase):
    """Base con el cliente HTTP y las dependencias mockeadas, igual que test_integration_momento_2.py."""

    def setUp(self):
        self.sfc_client_mock = MagicMock()
        self.s3_client_mock = MagicMock()
        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock

        self.idempotency_service_mock = MagicMock()
        self.idempotency_service_mock.verificar_o_iniciar_operacion = AsyncMock(return_value=(False, None))
        self.idempotency_service_mock.registrar_exito = AsyncMock()
        self.idempotency_service_mock.liberar_operacion_por_error = AsyncMock()

        processing_cm = MagicMock()
        processing_cm.__aenter__ = AsyncMock(return_value=None)
        processing_cm.__aexit__ = AsyncMock(return_value=False)
        self.idempotency_service_mock.mantener_processing_vivo = MagicMock(return_value=processing_cm)

        self.idempotency_service_patcher = patch(
            "app.api.routes_quejas.IdempotencyService", return_value=self.idempotency_service_mock
        )
        self.idempotency_service_patcher.start()

        self.client = TestClient(app)
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

        fecha_reciente = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self.payload = {
            "Smart_Code__c": "16551509974606",
            "CreatedDate": fecha_reciente,
            "Status": "New",
            "SuppliedName": "Camila Salas",
            "SC_id_type__c": "CC",
            "id_number__c": "1040011014",
            "tipo_de_persona__c": "B2C",
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "camila@test.com",
            "direccion__c": "Calle 93 # 11-11",
            "canal__c": "Internet",
            "punto_recepcion": "Manual",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "admision_col__c": "No Aplica",
            "Description": "Prueba de queja de integración.",
            "smart_anexo_queja__c": False,
            "Tutela__c": "No",
            "Ente_de_control__c": "Otros",
            "smart_escalamiento_DCF__c": "No",
            "Product__c": "Cuenta perfil",
            "smart_Producto_nombre__c": "Ahorro",
            "Categorias_COL__c": "Transacción no reconocida",
            "archivos_s3": [],
        }

    def tearDown(self):
        app.dependency_overrides.clear()
        self.idempotency_service_patcher.stop()


class TestDespachoIdempotenteHit(_RoutesQuejasHttpTestCase):

    def test_hit_idempotente_retorna_directamente_sin_llamar_al_orquestador(self):
        self.idempotency_service_mock.verificar_o_iniciar_operacion = AsyncMock(
            return_value=(True, {"status": "success", "sfc_response": {"Status": "ya procesado"}})
        )

        response = self.client.post("/api/v1/quejas/sync/despacho", json=self.payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("x-idempotent-hit"), "true")
        self.sfc_client_mock.post_nueva_queja.assert_not_called()


class TestAuditoriaEntradaCrmLoguearExtraData(_RoutesQuejasHttpTestCase):
    """
    🔴 FIX (hallazgo de revisión externa, 2026-08-25): el log
    AUDIT_HTTP_INCOMING_REQUEST_FROM_CRM pasaba `extra={...}` PLANO --
    JSONFormatter sólo lee `record.extra_data`, así que en producción el campo
    "extra" nunca aparecía en el JSON. Único rastro del lado de entrada de toda la
    cadena de auditoría regulatoria, perdido en silencio.
    """

    def test_log_de_auditoria_incluye_extra_data_con_el_body_y_headers(self):
        self.idempotency_service_mock.verificar_o_iniciar_operacion = AsyncMock(
            return_value=(True, {"status": "success", "sfc_response": {"Status": "ya procesado"}})
        )

        with self.assertLogs("app.api.routes_quejas", level="INFO") as logs:
            response = self.client.post("/api/v1/quejas/sync/despacho", json=self.payload)

        self.assertEqual(response.status_code, 200)

        registro_auditoria = next(
            r for r in logs.records if r.getMessage() == "AUDIT_HTTP_INCOMING_REQUEST_FROM_CRM"
        )
        self.assertTrue(hasattr(registro_auditoria, "extra_data"))
        self.assertEqual(registro_auditoria.extra_data["direction"], "INCOMING_REQUEST")
        # Smart_Code__c llega con el prefijo SFC_TIPO_ENTIDAD+SFC_ENTIDAD_COD ya
        # aplicado por el model_validator del schema (ver crm_payloads.py).
        self.assertTrue(
            registro_auditoria.extra_data["body"]["Smart_Code__c"].endswith(self.payload["Smart_Code__c"])
        )


class TestDespachoErrorDeOrquestacion(_RoutesQuejasHttpTestCase):

    def test_orquestador_retorna_status_error_da_400(self):
        with patch("app.api.routes_quejas.DespachoQuejaOrquestador") as mock_orq_cls:
            mock_orq_cls.return_value.procesar_despacho = AsyncMock(
                return_value={"status": "error", "message": "Catálogo inválido"}
            )
            response = self.client.post("/api/v1/quejas/sync/despacho", json=self.payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_type"], "DESPACHO_ORCHESTRATION_ERROR")
        self.idempotency_service_mock.liberar_operacion_por_error.assert_awaited_once()


class TestDespachoFalloPersistenciaIdempotenciaPostExito(_RoutesQuejasHttpTestCase):

    def test_fallo_al_registrar_exito_no_afecta_la_respuesta_200(self):
        self.idempotency_service_mock.registrar_exito = AsyncMock(side_effect=ConnectionError("redis caido"))

        with patch("app.api.routes_quejas.DespachoQuejaOrquestador") as mock_orq_cls, \
             patch("app.api.routes_quejas.EmailAlertService.notificar_falla_infraestructura", new=AsyncMock()) as mock_alerta:
            mock_orq_cls.return_value.procesar_despacho = AsyncMock(return_value={"status": "success"})
            response = self.client.post("/api/v1/quejas/sync/despacho", json=self.payload)

        self.assertEqual(response.status_code, 200)
        mock_alerta.assert_awaited_once()
        # La operación se considera exitosa/encolada -- no debe liberarse la llave de idempotencia.
        self.idempotency_service_mock.liberar_operacion_por_error.assert_not_awaited()


class TestDespachoExitosoCancelaItemDeColaObsoleto(_RoutesQuejasHttpTestCase):
    """
    🟢 FIX (hallazgo de code review, 2026-08-25): un despacho síncrono exitoso debe
    cancelar cualquier item de cola de contingencia que haya quedado pendiente para
    el mismo smart_code (contenido de un intento anterior fallido, ahora obsoleto) --
    si no, el scheduler lo reenviaría a la SFC más tarde, pudiendo pisar en silencio
    lo que este despacho más reciente ya corrigió.
    """

    def test_exito_sincrono_llama_a_cancelar_pendiente_con_el_smart_code_correcto(self):
        with patch("app.api.routes_quejas.DespachoQuejaOrquestador") as mock_orq_cls, \
             patch("app.api.routes_quejas.QueueService") as mock_queue_cls:
            mock_orq_cls.return_value.procesar_despacho = AsyncMock(return_value={"status": "success"})
            mock_queue_cls.return_value.cancelar_pendiente_por_smart_code = AsyncMock(return_value=True)

            response = self.client.post("/api/v1/quejas/sync/despacho", json=self.payload)

        self.assertEqual(response.status_code, 200)
        # Smart_Code__c llega con el prefijo SFC_TIPO_ENTIDAD+SFC_ENTIDAD_COD ya
        # aplicado por el model_validator del schema (ver crm_payloads.py) -- el
        # mismo valor prefijado que ya usa encolar_despacho/registrar_exito.
        smart_code_prefijado = f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}{self.payload['Smart_Code__c']}"
        mock_queue_cls.return_value.cancelar_pendiente_por_smart_code.assert_awaited_once_with(
            smart_code_prefijado
        )

    def test_fallo_al_cancelar_pendiente_no_afecta_la_respuesta_200(self):
        """Defensa en profundidad: aunque QueueService.cancelar_pendiente_por_smart_code
        ya es best-effort y no lanza por diseño, si algo inesperado lo hiciera lanzar
        igual, el despacho ya exitoso hacia el CRM no debe convertirse en un error --
        ver el try/except que envuelve esta llamada en routes_quejas.py."""
        with patch("app.api.routes_quejas.DespachoQuejaOrquestador") as mock_orq_cls, \
             patch("app.api.routes_quejas.QueueService") as mock_queue_cls:
            mock_orq_cls.return_value.procesar_despacho = AsyncMock(return_value={"status": "success"})
            mock_queue_cls.return_value.cancelar_pendiente_por_smart_code = AsyncMock(
                side_effect=ConnectionError("redis caido")
            )

            response = self.client.post("/api/v1/quejas/sync/despacho", json=self.payload)

        self.assertEqual(response.status_code, 200)
        self.idempotency_service_mock.liberar_operacion_por_error.assert_not_awaited()


class TestConsultarColaLocal(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)
        self.client.headers.update({"X-API-Key": settings.ADMIN_API_KEY})

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_lista_los_registros_encolados_como_resumen(self):
        item_mock = MagicMock()
        item_mock.to_summary_dict.return_value = {"id": 1, "smart_code": "SC-1", "estado": "PENDING"}

        with patch("app.api.routes_quejas.QueueService") as mock_queue_cls:
            mock_queue_cls.return_value.obtener_todos_los_encolados = AsyncMock(return_value=[item_mock])
            response = self.client.get("/api/v1/quejas/queue")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"id": 1, "smart_code": "SC-1", "estado": "PENDING"}])


if __name__ == "__main__":
    unittest.main()
