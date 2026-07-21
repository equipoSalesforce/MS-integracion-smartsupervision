# app/services/email_service.py
import logging
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List

from app.core.config import settings

logger = logging.getLogger(__name__)

class EmailAlertService:
    @staticmethod
    def _enviar_smtp_sync(destinatarios: List[str], asunto: str, cuerpo_html: str):
        """Método síncrono que realiza la conexión SMTP pura."""
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = asunto
            msg["From"] = settings.SMTP_USER
            msg["To"] = ", ".join(destinatarios)

            msg.attach(MIMEText(cuerpo_html, "html"))

            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                server.starttls()  # Seguridad TLS
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(settings.SMTP_USER, destinatarios, msg.as_string())
                
            logger.info(f"📧 [Email Alert] Alerta de infraestructura enviada exitosamente a: {destinatarios}")
        except Exception as e:
            logger.error(f"❌ [Email Alert] Error al enviar correo de alerta SMTP: {str(e)}")

    @classmethod
    async def notificar_falla_infraestructura(cls, smart_code: str, error_msg: str, ambiente: str = settings.ENVIRONMENT):
        """
        Notifica asíncronamente a ti y a tu TL cuando ocurre una falla de infraestructura 
        (SFC Caída / 502 / Timeout / Entrada a Cola SQLite).
        """
        if not settings.ALERT_EMAILS_ENABLED:
            return

        asunto = f"🚨 [ALERTA INFRA] SFC Caída / Caso encolado: {smart_code} [{ambiente.upper()}]"
        
        cuerpo_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="background-color: #d9534f; color: white; padding: 15px; border-radius: 5px;">
                    <h2 style="margin:0;">🚨 Alerta de Infraestructura - Microservicio SFC</h2>
                </div>
                <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px;">
                    <p>Se ha detectado una indisponibilidad o falla de red en la comunicación con la <strong>Superintendencia Financiera</strong>.</p>
                    <ul>
                        <li><strong>Ambiente:</strong> {ambiente.upper()}</li>
                        <li><strong>Smart Code Afectado:</strong> <code>{smart_code}</code></li>
                        <li><strong>Acción Tomada:</strong> Caso encolado automáticamente en SQLite local para reintento.</li>
                        <li><strong>Detalle del Error:</strong> <pre style="background: #f4f4f4; padding: 10px; border-radius: 4px;">{error_msg}</pre></li>
                    </ul>
                    <p style="font-size: 12px; color: #777;">Este es un mensaje automático de alerta de ingeniería.</p>
                </div>
            </body>
        </html>
        """

        # 🚀 Ejecutamos el envío en un hilo secundario para no demorar la respuesta de la API
        
        destinatarios = settings.ALERT_NOTIFY_EMAILS
        
        if settings.ENVIRONMENT == "local":
            destinatarios = ["juan.camargo@global66.com"]
        
        asyncio.create_task(
            asyncio.to_thread(
                cls._enviar_smtp_sync,
                destinatarios=destinatarios,
                asunto=asunto,
                cuerpo_html=cuerpo_html
            )
        )