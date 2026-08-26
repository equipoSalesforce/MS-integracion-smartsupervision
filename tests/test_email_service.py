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
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code="SC-1", error_msg="timeout", categoria="sfc_caida_contingencia"
            )
        mock_programar.assert_not_called()

    async def test_falla_infraestructura(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch.object(EmailAlertService, "_programar_envio_background") as mock_programar:
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code="SC-1", error_msg="timeout SFC", categoria="sfc_caida_contingencia"
            )
        mock_programar.assert_called_once()
        self.assertIn("SC-1", mock_programar.call_args.kwargs["asunto"])
        self.assertEqual(
            mock_programar.call_args.kwargs["clave_dedup"], "falla_infraestructura:sfc_caida_contingencia"
        )

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


class TestDedupClaveVentana(unittest.TestCase):
    """
    🟡 FIX (hallazgo A4, auditoría adversarial 2026-08-25): sin deduplicación,
    notificar_falla_infraestructura se dispara una vez por cada request mientras
    Redis está caído. _deberia_enviar es el mecanismo de clave+ventana que lo evita.
    """

    def setUp(self):
        EmailAlertService._ULTIMO_ENVIO_POR_CLAVE = {}

    def test_primera_llamada_con_clave_permite_envio(self):
        self.assertTrue(EmailAlertService._deberia_enviar("clave-x"))

    def test_segunda_llamada_dentro_de_la_ventana_no_permite_envio(self):
        EmailAlertService._deberia_enviar("clave-x")
        self.assertFalse(EmailAlertService._deberia_enviar("clave-x"))

    def test_clave_none_siempre_permite_envio(self):
        self.assertTrue(EmailAlertService._deberia_enviar(None))
        self.assertTrue(EmailAlertService._deberia_enviar(None))

    def test_claves_distintas_no_se_bloquean_entre_si(self):
        EmailAlertService._deberia_enviar("clave-a")
        self.assertTrue(EmailAlertService._deberia_enviar("clave-b"))

    def test_despues_de_la_ventana_vuelve_a_permitir_envio(self):
        with patch("app.services.email_service.time.monotonic", return_value=1000.0):
            self.assertTrue(EmailAlertService._deberia_enviar("clave-x"))
        with patch(
            "app.services.email_service.time.monotonic",
            return_value=1000.0 + EmailAlertService.VENTANA_DEDUP_SEGUNDOS + 1
        ):
            self.assertTrue(EmailAlertService._deberia_enviar("clave-x"))


class TestDedupCotaDeTamano(unittest.TestCase):
    """
    🔴 FIX (hallazgo N7, revisión externa v5, 2026-08-25): _ULTIMO_ENVIO_POR_CLAVE
    no tenía cota -- la clave de notificar_error_no_mapeado incorpora sfc_field
    (cardinalidad no controlada por este servicio, viene del JSON de error de la
    SFC), así que podía crecer sin límite durante la vida del proceso.
    """

    def setUp(self):
        EmailAlertService._ULTIMO_ENVIO_POR_CLAVE = {}

    def test_purgar_entradas_expiradas_elimina_solo_las_vencidas(self):
        with patch("app.services.email_service.time.monotonic", return_value=0.0):
            EmailAlertService._deberia_enviar("vieja")
        with patch(
            "app.services.email_service.time.monotonic",
            return_value=EmailAlertService.VENTANA_DEDUP_SEGUNDOS + 1
        ):
            EmailAlertService._deberia_enviar("nueva")
            EmailAlertService._purgar_entradas_expiradas()

        self.assertNotIn("vieja", EmailAlertService._ULTIMO_ENVIO_POR_CLAVE)
        self.assertIn("nueva", EmailAlertService._ULTIMO_ENVIO_POR_CLAVE)

    def test_no_crece_indefinidamente_mas_alla_del_cap(self):
        with patch.object(EmailAlertService, "MAX_ENTRADAS_DEDUP", 5):
            for i in range(50):
                EmailAlertService._deberia_enviar(f"clave-{i}")

            self.assertLessEqual(len(EmailAlertService._ULTIMO_ENVIO_POR_CLAVE), 5)

    def test_al_superar_el_cap_sin_expiradas_descarta_la_entrada_mas_vieja(self):
        with patch.object(EmailAlertService, "MAX_ENTRADAS_DEDUP", 3):
            for i, ts in enumerate([100.0, 200.0, 300.0]):
                with patch("app.services.email_service.time.monotonic", return_value=ts):
                    EmailAlertService._deberia_enviar(f"clave-{i}")

            # Las tres siguen vigentes (dentro de la ventana) -- no hay nada que
            # purgar por expiración, así que debe caer la más antigua (clave-0).
            with patch("app.services.email_service.time.monotonic", return_value=310.0):
                EmailAlertService._deberia_enviar("clave-nueva")

            self.assertNotIn("clave-0", EmailAlertService._ULTIMO_ENVIO_POR_CLAVE)
            self.assertIn("clave-1", EmailAlertService._ULTIMO_ENVIO_POR_CLAVE)
            self.assertIn("clave-2", EmailAlertService._ULTIMO_ENVIO_POR_CLAVE)
            self.assertIn("clave-nueva", EmailAlertService._ULTIMO_ENVIO_POR_CLAVE)


class TestProgramarEnvioBackgroundDedup(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        EmailAlertService._ULTIMO_ENVIO_POR_CLAVE = {}
        EmailAlertService._background_tasks = set()

    async def test_segunda_llamada_con_misma_clave_no_programa_tarea(self):
        with patch("app.services.email_service.asyncio.create_task") as mock_create_task, \
             patch("app.services.email_service.asyncio.to_thread", new=MagicMock()):
            mock_create_task.return_value = MagicMock()
            EmailAlertService._programar_envio_background(
                destinatarios=["ops@g66.com"], asunto="a", cuerpo_html="<p>a</p>", clave_dedup="clave-x"
            )
            EmailAlertService._programar_envio_background(
                destinatarios=["ops@g66.com"], asunto="b", cuerpo_html="<p>b</p>", clave_dedup="clave-x"
            )
        self.assertEqual(mock_create_task.call_count, 1)

    async def test_sin_clave_dedup_siempre_programa(self):
        with patch("app.services.email_service.asyncio.create_task") as mock_create_task, \
             patch("app.services.email_service.asyncio.to_thread", new=MagicMock()):
            mock_create_task.return_value = MagicMock()
            EmailAlertService._programar_envio_background(destinatarios=["ops@g66.com"], asunto="a", cuerpo_html="<p>a</p>")
            EmailAlertService._programar_envio_background(destinatarios=["ops@g66.com"], asunto="b", cuerpo_html="<p>b</p>")
        self.assertEqual(mock_create_task.call_count, 2)


class TestFallaInfraestructuraYErrorNoMapeadoDedupIntegracion(unittest.IsolatedAsyncioTestCase):
    """Verifica el enganche real de clave_dedup en los dos notificadores que
    señala el informe -- disparados por tráfico de request, no por un job programado."""

    def setUp(self):
        EmailAlertService._ULTIMO_ENVIO_POR_CLAVE = {}
        EmailAlertService._background_tasks = set()

    async def test_segunda_falla_infraestructura_en_la_ventana_no_reenvia(self):
        """Clave global por categoría: dos casos distintos durante la misma caída de
        Redis (misma categoría) sólo deben generar UN correo, no uno por smart_code."""
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.services.email_service.asyncio.create_task") as mock_create_task, \
             patch("app.services.email_service.asyncio.to_thread", new=MagicMock()):
            mock_create_task.return_value = MagicMock()
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code="SC-1", error_msg="timeout", categoria="redis_no_disponible"
            )
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code="SC-2", error_msg="timeout", categoria="redis_no_disponible"
            )
        self.assertEqual(mock_create_task.call_count, 1)

    async def test_falla_infraestructura_con_distinta_categoria_si_reenvia(self):
        """Hallazgo N7 residual (revisión externa v5): antes de este fix, una caída de
        Redis y un riesgo de duplicado post-SFC compartían la misma clave de dedup fija
        -- el primero en dispararse silenciaba al segundo durante 15 minutos, aunque
        fueran incidentes completamente distintos. Ahora cada categoría tiene su propia
        ventana."""
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.services.email_service.asyncio.create_task") as mock_create_task, \
             patch("app.services.email_service.asyncio.to_thread", new=MagicMock()):
            mock_create_task.return_value = MagicMock()
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code="SC-1", error_msg="redis caido", categoria="redis_no_disponible"
            )
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code="SC-1", error_msg="riesgo de duplicado", categoria="riesgo_duplicado_post_sfc"
            )
        self.assertEqual(mock_create_task.call_count, 2)

    async def test_error_no_mapeado_con_distinto_status_code_si_reenvia(self):
        """Distinta clave (status_code+campo) no debe deduplicarse entre sí."""
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.services.email_service.asyncio.create_task") as mock_create_task, \
             patch("app.services.email_service.asyncio.to_thread", new=MagicMock()):
            mock_create_task.return_value = MagicMock()
            await EmailAlertService.notificar_error_no_mapeado(status_code=500, raw_message="x", sfc_field="f")
            await EmailAlertService.notificar_error_no_mapeado(status_code=502, raw_message="x", sfc_field="f")
        self.assertEqual(mock_create_task.call_count, 2)

    async def test_error_no_mapeado_repetido_en_la_ventana_no_reenvia(self):
        with patch.object(settings, "ALERT_EMAILS_ENABLED", True), \
             patch("app.services.email_service.asyncio.create_task") as mock_create_task, \
             patch("app.services.email_service.asyncio.to_thread", new=MagicMock()):
            mock_create_task.return_value = MagicMock()
            await EmailAlertService.notificar_error_no_mapeado(
                status_code=500, raw_message="x", sfc_field="f", smart_code="SC-1"
            )
            await EmailAlertService.notificar_error_no_mapeado(
                status_code=500, raw_message="x", sfc_field="f", smart_code="SC-2"
            )
        self.assertEqual(mock_create_task.call_count, 1)


if __name__ == "__main__":
    unittest.main()
