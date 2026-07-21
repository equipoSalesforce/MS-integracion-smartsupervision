# app/services/email_service.py
import logging
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional

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
        
    # app/services/email_service.py (Añadir dentro de la clase EmailAlertService)

    @classmethod
    async def notificar_error_no_mapeado(
        cls, 
        status_code: int, 
        raw_message: str, 
        sfc_field: Optional[str] = None, 
        ambiente: str = settings.ENVIRONMENT
    ):
        """
        Notifica EXCLUSIVAMENTE al desarrollador (posición 0 de ALERT_NOTIFY_EMAILS)
        cuando la SFC devuelve un mensaje de error no reconocido en errores_sfc.json.
        """
        if not settings.ALERT_EMAILS_ENABLED or not settings.ALERT_NOTIFY_EMAILS:
            return

        # 🎯 Seleccionamos únicamente la posición 0 (Tu correo)
        destinatario_dev = [settings.ALERT_NOTIFY_EMAILS[0]]
        asunto = f"⚠️ [NUEVO ERROR NO MAPEADO SFC] HTTP {status_code} [{ambiente.upper()}]"

        cuerpo_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="background-color: #f0ad4e; color: white; padding: 15px; border-radius: 5px;">
                    <h2 style="margin:0;">⚠️ Nuevo Error No Mapeado de la SFC</h2>
                </div>
                <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px;">
                    <p>Hola Dev, la Superintendencia Financiera devolvió una respuesta que <strong>no coincide con ningún patrón</strong> en <code>errores_sfc.json</code>.</p>
                    
                    <table style="width: 100%; border-collapse: collapse; margin-top: 10px;">
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9; width: 30%;"><strong>Código HTTP:</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;"><code>{status_code}</code></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9;"><strong>Campo SFC (sfc_field):</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;"><code>{sfc_field or 'N/A (Cuerpo General)'}</code></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9;"><strong>Respuesta Raw SFC:</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;">
                                <pre style="background: #272822; color: #f8f8f2; padding: 10px; border-radius: 4px; overflow-x: auto;">{raw_message}</pre>
                            </td>
                        </tr>
                    </table>

                    <div style="margin-top: 15px; background-color: #eef7ff; padding: 12px; border-left: 4px solid #0275d8;">
                        💡 <strong>Acción recomendada:</strong> Copia la subcadena relevante de este error y agrégala a <code>app/core/errores_sfc.json</code> con su correspondiente diagnóstico para el CRM.
                    </div>
                </div>
            </body>
        </html>
        """

        # Dispatch asíncrono para no retrasar la respuesta HTTP
        asyncio.create_task(
            asyncio.to_thread(
                cls._enviar_smtp_sync,
                destinatarios=destinatario_dev,
                asunto=asunto,
                cuerpo_html=cuerpo_html
            )
        )