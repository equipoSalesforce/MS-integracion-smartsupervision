# tests/test_email_service.py
"""
Cobertura de app/services/email_service.py -- antes 58%. La mayoría de la
suite reemplaza EmailAlertService.notificar_* por AsyncMock para verificar
QUE se llama, sin ejercitar el cuerpo real (construcción de HTML, envío SMTP,
manejo de destinatarios). Se prueba _programar_envio_background mockeado (no
se abre un socket SMTP real) para cubrir la construcción de cada alerta, y
_enviar_smtp_sync/shutdown/_obtener_destinatarios por separado.
"""
import asyncio
import unittest
from unittest.mock import patch, MagicMock

from app.core.config import settings
from app.services.email_service import EmailAlertService, _esc, _limpiar_asunto


class TestHelpers(unittest.TestCase):

    def test_esc_none_retorna_vacio(self):
        self.assertEqual(_esc(None), "")

    def test_esc_escapa_html(self):
        self.assertEqual(_esc("<script>alert(1)</script>"), "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_esc_convierte_numeros(self):
        self.assertEqual(_esc(42), "42")

    def test_limpiar_asunto_remueve_saltos_de_linea(self):
        self.assertEqual(_limpiar_asunto("Alerta\r\nBcc: atacante@evil.com"), "AlertaBcc: atacante@evil.com")

    def test_limpiar_asunto_vacio(self):
        self.assertEqual(_limpiar_asunto(""), "")
        self.assertEqual(_limpiar_asunto(None), "")


class TestObtenerDestinatarios(unittest.TestCase):

    def test_todos_los_destinatarios_por_defecto(self):
        with patch.object(settings, "ALERT_NOTIFY_EMAILS", ["a@g66.com", "b@g66.com"]):
            self.assertEqual(EmailAlertService._obtener_destinatarios(), ["a@g66.com", "b@g66.com"])

    def test_solo_dev_retorna_solo_el_primero(self):
        with patch.object(settings, "ALERT_NOTIFY_EMAILS", ["dev@g66.com", "ops@g66.com"]):
            self.assertEqual(EmailAlertService._obtener_destinatarios(solo_dev=True), ["dev@g66.com"])

    def test_sin_destinatarios_configurados_retorna_lista_vacia(self):
        with patch.object(settings, "ALERT_NOTIFY_EMAILS", []):
            self.assertEqual(EmailAlertService._obtener_destinatarios(), [])
            self.assertEqual(EmailAlertService._obtener_destinatarios(solo_dev=True), [])


class TestEnviarSmtpSync(unittest.TestCase):

    def test_sin_destinatarios_no_intenta_conectar(self):
        with patch("app.services.email_service.smtplib.SMTP") as mock_smtp:
            EmailAlertService._enviar_smtp_sync(destinatarios=[], asunto="x", cuerpo_html="<p>x</p>")
        mock_smtp.assert_not_called()

    def test_envio_exitoso_llama_starttls_login_y_sendmail(self):
        mock_server = MagicMock()
        mock_server.__enter__.return_value = mock_server
        with patch("app.services.email_service.smtplib.SMTP", return_value=mock_server):
            EmailAlertService._enviar_smtp_sync(
                destinatarios=["ops@g66.com"], asunto="Asunto\r\ncon salto", cuerpo_html="<p>hola</p>"
            )

        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with(settings.SMTP_USER, settings.SMTP_PASSWORD)
        mock_server.sendmail.assert_called_once()
        args = mock_server.sendmail.call_args[0]
        self.assertEqual(args[1], ["ops@g66.com"])

    def test_error_smtp_no_propaga(self):
        with patch("app.services.email_service.smtplib.SMTP", side_effect=OSError("conexión rechazada")):
            EmailAlertService._enviar_smtp_sync(
                destinatarios=["ops@g66.com"], asunto="x", cuerpo_html="<p>x</p>"
            )  # No debe lanzar.


class TestShutdown(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        EmailAlertService._background_tasks = set()

    async def test_sin_tareas_pendientes_retorna_de_inmediato(self):
        await EmailAlertService.shutdown(timeout_segundos=1.0)  # No debe lanzar ni bloquear.

    async def test_espera_tareas_en_vuelo_hasta_completarse(self):
        async def _tarea_rapida():
            await asyncio.sleep(0.01)

        task = asyncio.create_task(_tarea_rapida())
        EmailAlertService._background_tasks.add(task)

        await EmailAlertService.shutdown(timeout_segundos=1.0)

        self.assertTrue(task.done())

    async def test_timeout_no_propaga_si_tarea_no_termina_a_tiempo(self):
        async def _tarea_lenta():
            await asyncio.sleep(5)

        task = asyncio.create_task(_tarea_lenta())
        EmailAlertService._background_tasks.add(task)

        await EmailAlertService.shutdown(timeout_segundos=0.05)  # No debe lanzar TimeoutError.

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestNotificarMetodos(unittest.IsolatedAsyncioTestCase):
    """
    Cubre la construcción real (asunto + cuerpo HTML) de cada notificar_* con
    ALERT_EMAILS_ENABLED=True, mockeando sólo el envío de fondo -- y la rama
    temprana compartida (ALERT_EMAILS_ENABLED=False) una vez, ya que el patrón
    `if not settings.ALERT_EMAILS_ENABLED: return` es idéntico en los 8 métodos.
    """

    async def test_deshabilitado_no_programa_envio(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", False), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_falla_infraestructura(smart_code="SC-1", error_msg="timeout")
        mock_programar.assert_not_called()

    async def test_falla_infraestructura(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_falla_infraestructura(smart_code="SC-1", error_msg="timeout SFC")
        mock_programar.assert_called_once()
        self.assertIn("SC-1", mock_programar.call_args.kwargs["asunto"])

    async def test_error_no_mapeado(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_error_no_mapeado(
                status_code=500, raw_message="<xml>error</xml>", sfc_field="numero_id_CF", smart_code="SC-2"
            )
        mock_programar.assert_called_once()
        self.assertIn("500", mock_programar.call_args.kwargs["asunto"])
        # El body debe venir escapado (sin las llaves angulares crudas del XML).
        self.assertNotIn("<xml>", mock_programar.call_args.kwargs["cuerpo_html"])

    async def test_umbral_cola(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_umbral_cola(total_pendientes=100)
        mock_programar.assert_called_once()
        self.assertIn("100", mock_programar.call_args.kwargs["asunto"])

    async def test_casos_vencimiento_sla_con_casos(self):
        casos = [{
            "smart_code": "SC-3", "correlation_id": "cid-1", "fecha_encolado": "2026-08-01",
            "horas_en_cola": 14.5, "reintentos": 3, "ultimo_error": "timeout"
        }]
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_casos_vencimiento_sla(casos_vencidos=casos)
        mock_programar.assert_called_once()
        self.assertIn("SC-3", mock_programar.call_args.kwargs["cuerpo_html"])

    async def test_casos_vencimiento_sla_lista_vacia_no_notifica(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_casos_vencimiento_sla(casos_vencidos=[])
        mock_programar.assert_not_called()

    async def test_caso_fallido_definitivo(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_caso_fallido_definitivo(
                smart_code="SC-4", total_intentos=10, ultimo_error="SFC no disponible"
            )
        mock_programar.assert_called_once()
        self.assertIn("SC-4", mock_programar.call_args.kwargs["asunto"])

    async def test_recuperacion_sfc(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_recuperacion_sfc(total_despachados=25)
        mock_programar.assert_called_once()
        self.assertIn("25", mock_programar.call_args.kwargs["asunto"])

    async def test_catalogo_stale(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_catalogo_stale(
                nombre_componente="SfcErrorTranslator", edad_horas=25.3, error_msg="Google Sheets caído"
            )
        mock_programar.assert_called_once()
        self.assertIn("SfcErrorTranslator", mock_programar.call_args.kwargs["asunto"])


if __name__ == "__main__":
    unittest.main()
