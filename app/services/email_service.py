# app/services/email_service.py
import logging
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional, Dict, Any

from app.core.config import settings
from app.core.middleware import get_correlation_id

logger = logging.getLogger(__name__)


class EmailAlertService:

    @classmethod
    def _obtener_destinatarios(cls, solo_dev: bool = False) -> List[str]:
        """Resuelve los destinatarios según el entorno o si la alerta es exclusiva para Dev."""
        if settings.ENVIRONMENT == "local":
            return ["juan.camargo@global66.com"]
        if solo_dev and settings.ALERT_NOTIFY_EMAILS:
            return [settings.ALERT_NOTIFY_EMAILS[0]]
        return settings.ALERT_NOTIFY_EMAILS or []

    @staticmethod
    def _enviar_smtp_sync(destinatarios: List[str], asunto: str, cuerpo_html: str):
        """Método síncrono que realiza la conexión SMTP pura."""
        if not destinatarios:
            logger.warning("⚠️ [Email Alert] No hay destinatarios configurados. Se omite envío.")
            return

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = asunto
            msg["From"] = settings.SMTP_USER
            msg["To"] = ", ".join(destinatarios)

            msg.attach(MIMEText(cuerpo_html, "html"))

            # 🟢 FIX: Se añade timeout=10 para evitar bloqueos indefinidos de hilos en caso de problemas de red/VPC
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
                server.starttls()
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(settings.SMTP_USER, destinatarios, msg.as_string())

            logger.info(f"📧 [Email Alert] Alerta enviada a: {destinatarios}")
        except Exception as e:
            logger.error(f"❌ [Email Alert] Error al enviar correo SMTP: {str(e)}")

    # =========================================================================
    # 🚨 1. NOTIFICACIÓN DE INDISPONIBILIDAD INICIAL DE INFRAESTRUCTURA
    # =========================================================================
    @classmethod
    async def notificar_falla_infraestructura(
        cls, 
        smart_code: str, 
        error_msg: str, 
        correlation_id: Optional[str] = None,
        ambiente: str = settings.ENVIRONMENT
    ):
        """Notifica asíncronamente cuando ocurre un corte de red/SFC 502 y un caso entra a la cola."""
        if not settings.ALERT_EMAILS_ENABLED:
            return

        cid = correlation_id or get_correlation_id()
        asunto = f"🚨 [ALERTA INFRA] SFC Caída / Caso: {smart_code} | CID: {cid} [{ambiente.upper()}]"

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
                        <li><strong>Correlation ID (CID):</strong> <strong style="color: #0275d8;"><code>{cid}</code></strong></li>
                        <li><strong>Smart Code Afectado:</strong> <code>{smart_code}</code></li>
                        <li><strong>Acción Tomada:</strong> Caso encolado automáticamente en Redis centralizado para reintento.</li>
                        <li><strong>Detalle del Error:</strong> <pre style="background: #f4f4f4; padding: 10px; border-radius: 4px;">{error_msg}</pre></li>
                    </ul>
                    <p style="font-size: 12px; color: #777;">Este es un mensaje automático de alerta de ingeniería. Use el Correlation ID para rastrear los logs en CloudWatch.</p>
                </div>
            </body>
        </html>
        """

        asyncio.create_task(
            asyncio.to_thread(
                cls._enviar_smtp_sync,
                destinatarios=cls._obtener_destinatarios(),
                asunto=asunto,
                cuerpo_html=cuerpo_html
            )
        )

    # =========================================================================
    # ⚠️ 2. NOTIFICACIÓN DE ERROR NO MAPEADO (SOLO PARA DEV)
    # =========================================================================
    @classmethod
    async def notificar_error_no_mapeado(
        cls,
        status_code: int,
        raw_message: str,
        sfc_field: Optional[str] = None,
        smart_code: Optional[str] = None,
        correlation_id: Optional[str] = None,
        ambiente: str = settings.ENVIRONMENT
    ):
        """Notifica EXCLUSIVAMENTE al desarrollador cuando la SFC devuelve un error desconocido."""
        if not settings.ALERT_EMAILS_ENABLED:
            return

        cid = correlation_id or get_correlation_id()
        destinatario_dev = cls._obtener_destinatarios(solo_dev=True)
        asunto = f"⚠️ [ERROR NO MAPEADO SFC] HTTP {status_code} | Caso: {smart_code or 'N/A'} | CID: {cid} [{ambiente.upper()}]"

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
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9; width: 30%;"><strong>Correlation ID (CID):</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;"><strong style="color: #0275d8;"><code>{cid}</code></strong></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9;"><strong>Smart Code / Caso:</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;"><code>{smart_code or 'N/A'}</code></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9;"><strong>Código HTTP:</strong></td>
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
                        💡 <strong>Acción recomendada:</strong> Copia el Correlation ID <code>{cid}</code> para filtrar la traza en CloudWatch, revisa el payload y agrega la subcadena relevante a <code>errores_sfc.json</code>.
                    </div>
                </div>
            </body>
        </html>
        """

        asyncio.create_task(
            asyncio.to_thread(
                cls._enviar_smtp_sync,
                destinatarios=destinatario_dev,
                asunto=asunto,
                cuerpo_html=cuerpo_html
            )
        )

    # =========================================================================
    # 📊 3. NOTIFICACIÓN POR UMBRAL DE VOLUMEN (CADA 100 EN COLA)
    # =========================================================================
    @classmethod
    async def notificar_umbral_cola(
        cls, total_pendientes: int, ambiente: str = settings.ENVIRONMENT
    ):
        """Notifica cuando la acumulación de casos retenidos en la cola alcanza múltiplos de 100."""
        if not settings.ALERT_EMAILS_ENABLED:
            return

        asunto = f"📊 [ALERTA COLA] Acumulación en Cola: {total_pendientes} casos pendientes [{ambiente.upper()}]"

        cuerpo_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="background-color: #f0ad4e; color: white; padding: 15px; border-radius: 5px;">
                    <h2 style="margin:0;">📊 Umbral de Acumulación en Cola Alcanzado</h2>
                </div>
                <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px;">
                    <p>La cola de contingencia de SmartSupervision ha alcanzado un nuevo volumen crítico de acumulados.</p>
                    <ul>
                        <li><strong>Ambiente:</strong> {ambiente.upper()}</li>
                        <li><strong>Casos Pendientes en Cola:</strong> <span style="font-size: 18px; font-weight: bold; color: #d9534f;">{total_pendientes}</span></li>
                        <li><strong>Estado SFC:</strong> Intermitente / Caída.</li>
                    </ul>
                    <p>El worker automático continuará intentando el despacho en segundo plano. Si el servicio de la SFC permanece caído, los casos se mantendrán en custodia segura dentro de Redis.</p>
                    <p style="font-size: 12px; color: #777;">Notificación automática por hito de volumen de contingencia.</p>
                </div>
            </body>
        </html>
        """

        asyncio.create_task(
            asyncio.to_thread(
                cls._enviar_smtp_sync,
                destinatarios=cls._obtener_destinatarios(),
                asunto=asunto,
                cuerpo_html=cuerpo_html
            )
        )

    # =========================================================================
    # ⏳ 4. DIGEST DE CASOS VENCIDOS O PRÓXIMOS A VENCER (> 12 HORAS)
    # =========================================================================
    @classmethod
    async def notificar_casos_vencimiento_sla(
        cls, casos_vencidos: List[Dict[str, Any]], ambiente: str = settings.ENVIRONMENT
    ):
        """Genera un correo digest con tabla de casos que llevan más de 12 horas en la cola."""
        if not settings.ALERT_EMAILS_ENABLED or not casos_vencidos:
            return

        total = len(casos_vencidos)
        asunto = f"⏳ [ALERTA SLA] {total} Caso(s) con > 12h en Cola de Contingencia [{ambiente.upper()}]"

        filas_tabla = ""
        for c in casos_vencidos:
            smart_code = c.get("smart_code", "N/A")
            cid_caso = c.get("correlation_id", "N/A")
            fecha_encolado = c.get("fecha_encolado", "N/A")
            horas_cola = c.get("horas_en_cola", 0)
            reintentos = c.get("reintentos", 0)
            ultimo_error = c.get("ultimo_error", "Sin detalle")[:100]

            filas_tabla += f"""
            <tr>
                <td style="padding: 8px; border: 1px solid #ddd;"><code>{smart_code}</code></td>
                <td style="padding: 8px; border: 1px solid #ddd;"><code style="color: #0275d8;">{cid_caso}</code></td>
                <td style="padding: 8px; border: 1px solid #ddd;">{fecha_encolado}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align: center; font-weight: bold; color: #d9534f;">{horas_cola:.1f} hrs</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{reintentos}</td>
                <td style="padding: 8px; border: 1px solid #ddd; font-size: 12px;">{ultimo_error}</td>
            </tr>
            """

        cuerpo_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="background-color: #d9534f; color: white; padding: 15px; border-radius: 5px;">
                    <h2 style="margin:0;">⏳ Alerta de Envejecimiento de Casos (> 12 Horas)</h2>
                </div>
                <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px;">
                    <p>Se han identificado <strong>{total} caso(s)</strong> que superan las 12 horas de retención en la cola Redis sin haber podido transmitirse a la Superintendencia Financiera.</p>
                    
                    <table style="width: 100%; border-collapse: collapse; margin-top: 15px;">
                        <thead>
                            <tr style="background-color: #f2f2f2; text-align: left;">
                                <th style="padding: 8px; border: 1px solid #ddd;">Smart Code</th>
                                <th style="padding: 8px; border: 1px solid #ddd;">Correlation ID</th>
                                <th style="padding: 8px; border: 1px solid #ddd;">Fecha Encolado</th>
                                <th style="padding: 8px; border: 1px solid #ddd;">Tiempo Retenido</th>
                                <th style="padding: 8px; border: 1px solid #ddd;">Reintentos</th>
                                <th style="padding: 8px; border: 1px solid #ddd;">Último Error</th>
                            </tr>
                        </thead>
                        <tbody>
                            {filas_tabla}
                        </tbody>
                    </table>

                    <p style="margin-top: 20px; font-size: 12px; color: #777;">Reporte automático de control de SLA operativo.</p>
                </div>
            </body>
        </html>
        """

        asyncio.create_task(
            asyncio.to_thread(
                cls._enviar_smtp_sync,
                destinatarios=cls._obtener_destinatarios(),
                asunto=asunto,
                cuerpo_html=cuerpo_html
            )
        )

    # =========================================================================
    # ❌ 5. NOTIFICACIÓN DE CASO FALLIDO DEFINITIVO (DEAD LETTER QUEUE)
    # =========================================================================
    @classmethod
    async def notificar_caso_fallido_definitivo(
        cls,
        smart_code: str,
        total_intentos: int,
        ultimo_error: str,
        correlation_id: Optional[str] = None,
        ambiente: str = settings.ENVIRONMENT
    ):
        """Notifica de inmediato cuando un caso agota todos sus reintentos y entra en FALLIDO_DEFINITIVO (DLQ)."""
        if not settings.ALERT_EMAILS_ENABLED:
            return

        cid = correlation_id or get_correlation_id()
        asunto = f"❌ [ALERTA DLQ] Caso Fallido Definitivo: {smart_code} | CID: {cid} [{ambiente.upper()}]"

        cuerpo_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="background-color: #8b0000; color: white; padding: 15px; border-radius: 5px;">
                    <h2 style="margin:0;">❌ Caso Descartado de Cola (Dead Letter Queue)</h2>
                </div>
                <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px;">
                    <p>Un caso ha alcanzado el límite máximo de reintentos de despacho a la <strong>Superintendencia Financiera</strong> y ha pasado al estado <strong>FALLIDO_DEFINITIVO</strong>.</p>
                    <ul>
                        <li><strong>Ambiente:</strong> {ambiente.upper()}</li>
                        <li><strong>Correlation ID (CID):</strong> <strong style="color: #0275d8;"><code>{cid}</code></strong></li>
                        <li><strong>Smart Code:</strong> <code>{smart_code}</code></li>
                        <li><strong>Total Intentos Agotados:</strong> {total_intentos}</li>
                        <li><strong>Último Error Registrado:</strong> <pre style="background: #f4f4f4; padding: 10px; border-radius: 4px;">{ultimo_error}</pre></li>
                    </ul>
                    <p style="color: #d9534f; font-weight: bold;">⚠️ El Scheduler dejará de reintentar este caso automáticamente. Requiere revisión e intervención manual en Redis.</p>
                    <p style="font-size: 12px; color: #777;">Notificación crítica automática por agotamiento de reintentos de contingencia.</p>
                </div>
            </body>
        </html>
        """

        asyncio.create_task(
            asyncio.to_thread(
                cls._enviar_smtp_sync,
                destinatarios=cls._obtener_destinatarios(),
                asunto=asunto,
                cuerpo_html=cuerpo_html
            )
        )

    # =========================================================================
    # ✅ 6. CONFIRMACIÓN DE RESTABLECIMIENTO Y RECUPERACIÓN (SFC ONLINE)
    # =========================================================================
    @classmethod
    async def notificar_recuperacion_sfc(
        cls, total_despachados: int, ambiente: str = settings.ENVIRONMENT
    ):
        """Notifica cuando la SFC vuelve a estar online y se ha vaciado la cola retenida."""
        if not settings.ALERT_EMAILS_ENABLED:
            return

        asunto = f"✅ [AUTORRECUPERACIÓN] Conexión SFC Restablecida ({total_despachados} casos transmitidos) [{ambiente.upper()}]"

        cuerpo_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="background-color: #5cb85c; color: white; padding: 15px; border-radius: 5px;">
                    <h2 style="margin:0;">✅ Servicio SFC Restablecido Exitosamente</h2>
                </div>
                <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px;">
                    <p>La comunicación con la <strong>Superintendencia Financiera</strong> se ha restablecido correctamente y la cola de contingencia ha sido procesada.</p>
                    <ul>
                        <li><strong>Ambiente:</strong> {ambiente.upper()}</li>
                        <li><strong>Casos Transmitidos Exitosamente:</strong> <strong style="color: #5cb85c; font-size: 16px;">{total_despachados}</strong></li>
                        <li><strong>Estado Actual de la Cola:</strong> Vacía / Operación Normal.</li>
                    </ul>
                    <p>Todos los acuses de recibo devueltos por la SFC han sido registrados correctamente en Redis.</p>
                    <p style="font-size: 12px; color: #777;">Notificación automática de autorrecuperación de servicio.</p>
                </div>
            </body>
        </html>
        """

        asyncio.create_task(
            asyncio.to_thread(
                cls._enviar_smtp_sync,
                destinatarios=cls._obtener_destinatarios(),
                asunto=asunto,
                cuerpo_html=cuerpo_html
            )
        )