# tests/test_email_smtp_sender.py
"""
Auditoría 2026-08-13 (P1-08): en AWS SES, SMTP_USER es una credencial IAM
generada, no una dirección de correo entregable — usarla como remitente puede
fallar la entrega o DMARC. SMTP_FROM_EMAIL debe ser el remitente visible,
separado de la credencial de autenticación SMTP_USER.
"""
import unittest
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.services.email_service import EmailAlertService


class TestEmailSmtpSender(unittest.TestCase):

    def _mock_smtp_server(self):
        server = MagicMock()
        server.__enter__ = MagicMock(return_value=server)
        server.__exit__ = MagicMock(return_value=False)
        return server

    def test_smtp_from_email_configurado_se_usa_como_remitente(self):
        """Con SMTP_FROM_EMAIL definido, se usa como From y como sobre (envelope) del correo."""
        server = self._mock_smtp_server()
        with patch.object(settings, "SMTP_FROM_EMAIL", "alertas@global66.com"), \
             patch.object(settings, "SMTP_USER", "ses-smtp-user-iam-id"), \
             patch.object(settings, "SMTP_PASSWORD", "secreto"), \
             patch("app.services.email_service.smtplib.SMTP", return_value=server) as mock_smtp_cls:

            EmailAlertService._enviar_smtp_sync(
                destinatarios=["ops@global66.com"],
                asunto="Prueba",
                cuerpo_html="<p>Contenido</p>"
            )

        mock_smtp_cls.assert_called_once()
        server.login.assert_called_once_with("ses-smtp-user-iam-id", "secreto")

        args, _ = server.sendmail.call_args
        remitente_sendmail, destinatarios, _ = args
        self.assertEqual(remitente_sendmail, "alertas@global66.com")
        self.assertEqual(destinatarios, ["ops@global66.com"])

    def test_smtp_from_email_ausente_usa_smtp_user_como_remitente(self):
        """Sin SMTP_FROM_EMAIL, cae de vuelta a SMTP_USER (compatibilidad con Gmail/entornos previos)."""
        server = self._mock_smtp_server()
        with patch.object(settings, "SMTP_FROM_EMAIL", None), \
             patch.object(settings, "SMTP_USER", "alguien@global66.com"), \
             patch.object(settings, "SMTP_PASSWORD", "secreto"), \
             patch("app.services.email_service.smtplib.SMTP", return_value=server):

            EmailAlertService._enviar_smtp_sync(
                destinatarios=["ops@global66.com"],
                asunto="Prueba",
                cuerpo_html="<p>Contenido</p>"
            )

        args, _ = server.sendmail.call_args
        remitente_sendmail, _, _ = args
        self.assertEqual(remitente_sendmail, "alguien@global66.com")
        server.login.assert_called_once_with("alguien@global66.com", "secreto")

    def test_autenticacion_smtp_siempre_usa_smtp_user_no_el_remitente_visible(self):
        """
        La autenticación (login) SIEMPRE debe usar la credencial real (SMTP_USER),
        nunca el remitente visible SMTP_FROM_EMAIL — son conceptos distintos en SES.
        """
        server = self._mock_smtp_server()
        with patch.object(settings, "SMTP_FROM_EMAIL", "alertas@global66.com"), \
             patch.object(settings, "SMTP_USER", "ses-smtp-user-iam-id"), \
             patch.object(settings, "SMTP_PASSWORD", "secreto"), \
             patch("app.services.email_service.smtplib.SMTP", return_value=server):

            EmailAlertService._enviar_smtp_sync(
                destinatarios=["ops@global66.com"],
                asunto="Prueba",
                cuerpo_html="<p>Contenido</p>"
            )

        login_user, login_pass = server.login.call_args[0]
        self.assertEqual(login_user, "ses-smtp-user-iam-id")
        self.assertNotEqual(login_user, "alertas@global66.com")


if __name__ == "__main__":
    unittest.main()
