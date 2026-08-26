# app/services/email_service.py
import html
import logging
import smtplib
import asyncio
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional, Dict, Any, Set

from app.core.config import settings
from app.core.middleware import get_correlation_id

logger = logging.getLogger(__name__)


def _esc(val: Any) -> str:
    """Escapa caracteres especiales HTML de cualquier valor."""
    if val is None:
        return ""
    return html.escape(str(val))


def _limpiar_asunto(asunto: str) -> str:
    """Remueve saltos de línea para prevenir inyección de encabezados SMTP."""
    if not asunto:
        return ""
    return str(asunto).replace("\r", "").replace("\n", "").strip()


class EmailAlertService:
    # 🟢 RASTREO DE TAREAS EN SEGUNDO PLANO: Evita advertencias de tareas destruidas
    _background_tasks: Set[asyncio.Task] = set()

    # 🟡 FIX (hallazgo A4, auditoría adversarial 2026-08-25): sin esto, una caída de
    # Redis dispara notificar_falla_infraestructura EN CADA REQUEST (vía
    # idempotency_service.py::_fail_closed_redis_no_disponible) -- cada uno bloquea
    # hasta 10s en SMTP dentro del pool de hilos compartido con S3/PDF, y la bandeja
    # de operaciones recibe un correo idéntico por request. Se deduplica por
    # clave+ventana: la misma clave no vuelve a disparar un envío hasta que pase la
    # ventana, sin importar cuántas veces se llame al notificador mientras tanto.
    _ULTIMO_ENVIO_POR_CLAVE: Dict[str, float] = {}
    VENTANA_DEDUP_SEGUNDOS = 900  # 15 minutos

    # 🔴 FIX (hallazgo N7, revisión externa v5, 2026-08-25): _ULTIMO_ENVIO_POR_CLAVE
    # no tenía cota de tamaño -- la clave de notificar_error_no_mapeado incorpora
    # sfc_field, que _extraer_informacion_error arma a partir de las claves del JSON
    # de error de la SFC (cardinalidad no controlada por este servicio), así que el
    # diccionario podía crecer sin límite durante toda la vida del proceso. Se acota
    # con una purga best-effort: nunca bloquea ni pierde una alerta real, en el peor
    # caso una clave poco común se re-envía antes de los 15 minutos ideales.
    MAX_ENTRADAS_DEDUP = 500

    @classmethod
    def _purgar_entradas_expiradas(cls) -> None:
        ahora = time.monotonic()
        expiradas = [
            clave for clave, ts in cls._ULTIMO_ENVIO_POR_CLAVE.items()
            if (ahora - ts) >= cls.VENTANA_DEDUP_SEGUNDOS
        ]
        for clave in expiradas:
            del cls._ULTIMO_ENVIO_POR_CLAVE[clave]

    @classmethod
    def _deberia_enviar(cls, clave_dedup: Optional[str]) -> bool:
        if clave_dedup is None:
            return True
        ahora = time.monotonic()
        ultimo_envio = cls._ULTIMO_ENVIO_POR_CLAVE.get(clave_dedup)
        if ultimo_envio is not None and (ahora - ultimo_envio) < cls.VENTANA_DEDUP_SEGUNDOS:
            return False

        if len(cls._ULTIMO_ENVIO_POR_CLAVE) >= cls.MAX_ENTRADAS_DEDUP:
            cls._purgar_entradas_expiradas()
            if len(cls._ULTIMO_ENVIO_POR_CLAVE) >= cls.MAX_ENTRADAS_DEDUP:
                # Todavía por encima del cap tras purgar expiradas -- se descarta la
                # entrada más antigua en vez de crecer sin límite.
                clave_mas_vieja = min(cls._ULTIMO_ENVIO_POR_CLAVE, key=cls._ULTIMO_ENVIO_POR_CLAVE.get)
                del cls._ULTIMO_ENVIO_POR_CLAVE[clave_mas_vieja]

        cls._ULTIMO_ENVIO_POR_CLAVE[clave_dedup] = ahora
        return True

    @classmethod
    def _programar_envio_background(
        cls, destinatarios: List[str], asunto: str, cuerpo_html: str, clave_dedup: Optional[str] = None
    ):
        """Programa la tarea de envío y mantiene la referencia viva hasta su finalización."""
        if not cls._deberia_enviar(clave_dedup):
            logger.info(
                f"📧 [Email Alert] Envío omitido por deduplicación (clave='{clave_dedup}', "
                f"ventana={cls.VENTANA_DEDUP_SEGUNDOS}s): '{asunto}'"
            )
            return

        task = asyncio.create_task(
            asyncio.to_thread(
                cls._enviar_smtp_sync,
                destinatarios=destinatarios,
                asunto=asunto,
                cuerpo_html=cuerpo_html
            )
        )
        cls._background_tasks.add(task)
        task.add_done_callback(cls._background_tasks.discard)

    @classmethod
    async def shutdown(cls, timeout_segundos: float = 3.0):
        """
        🟢 APAGADO CONTROLADO (GRACEFUL SHUTDOWN):
        Durante el despliegue/reinicio en ECS, espera un máximo de `timeout_segundos`
        a que las alertas por correo en vuelo terminen de enviarse.
        """
        if not cls._background_tasks:
            return

        logger.info(f"⏳ [Email Alert] Esperando finalización de {len(cls._background_tasks)} alerta(s) de correo en vuelo...")
        try:
            await asyncio.wait_for(
                asyncio.gather(*list(cls._background_tasks), return_exceptions=True),
                timeout=timeout_segundos
            )
            logger.info("✅ [Email Alert] Tareas de correo en segundo plano finalizadas limpiamente.")
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ [Email Alert] Se alcanzó el tiempo límite de {timeout_segundos}s durante el apagado.")

    @classmethod
    def _obtener_destinatarios(cls, solo_dev: bool = False) -> List[str]:
        if solo_dev and settings.ALERT_NOTIFY_EMAILS:
            return [settings.ALERT_NOTIFY_EMAILS[0]]
        return settings.ALERT_NOTIFY_EMAILS or []

    @staticmethod
    def _enviar_smtp_sync(destinatarios: List[str], asunto: str, cuerpo_html: str):
        if not destinatarios:
            logger.warning("⚠️ [Email Alert] No hay destinatarios configurados. Se omite envío.")
            return

        try:
            # 🟢 FIX P1-08: remitente separado de la credencial de autenticación SMTP —
            # en SES, SMTP_USER es un ID IAM generado, no una dirección entregable.
            remitente = settings.SMTP_FROM_EMAIL or settings.SMTP_USER

            msg = MIMEMultipart("alternative")
            msg["Subject"] = _limpiar_asunto(asunto)
            msg["From"] = remitente
            msg["To"] = ", ".join(destinatarios)

            msg.attach(MIMEText(cuerpo_html, "html"))

            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
                server.starttls()
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(remitente, destinatarios, msg.as_string())

            logger.info(f"📧 [Email Alert] Alerta enviada a: {destinatarios}")
        except Exception as e:
            logger.error(f"❌ [Email Alert] Error al enviar correo SMTP: {str(e)}")

    @classmethod
    async def notificar_falla_infraestructura(
        cls,
        smart_code: str,
        error_msg: str,
        categoria: str,
        correlation_id: Optional[str] = None,
        ambiente: str = settings.ENVIRONMENT
    ):
        """
        `categoria` identifica el TIPO de falla de infraestructura (ej.
        "redis_no_disponible", "riesgo_duplicado_post_sfc", "sfc_caida_contingencia").

        🟢 FIX (N7 residual, revisión externa v5, 2026-08-25): antes todos los
        call sites (9 en total, desde Redis caído hasta el healthcheck del worker)
        compartían la misma clave de dedup fija ("falla_infraestructura") -- durante
        la misma ventana de 15 minutos, el primero en dispararse silenciaba a
        TODOS los demás, incluida "riesgo de duplicado" tras una persistencia
        post-SFC fallida (el más accionable de todos). Cada categoría ahora tiene
        su propia ventana de dedup independiente; sigue siendo global por
        categoría (no por smart_code) -- una caída de infraestructura del mismo
        tipo es un solo evento, no N eventos independientes por cada caso que la
        sufre.
        """
        if not settings.ALERT_EMAILS_ENABLED:
            return

        cid = correlation_id or get_correlation_id() or "N/A"
        asunto = f"🚨 [ALERTA INFRA:{categoria}] SFC Caída / Caso: {smart_code} | CID: {cid} [{ambiente.upper()}]"

        cuerpo_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="background-color: #d9534f; color: white; padding: 15px; border-radius: 5px;">
                    <h2 style="margin:0;">🚨 Alerta de Infraestructura - Microservicio SFC</h2>
                </div>
                <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px;">
                    <p>Se ha detectado una indisponibilidad o falla de red en la comunicación con la <strong>Superintendencia Financiera</strong>.</p>
                    <ul>
                        <li><strong>Categoría:</strong> <code>{_esc(categoria)}</code></li>
                        <li><strong>Ambiente:</strong> {_esc(ambiente.upper())}</li>
                        <li><strong>Correlation ID (CID):</strong> <strong style="color: #0275d8;"><code>{_esc(cid)}</code></strong></li>
                        <li><strong>Smart Code Afectado:</strong> <code>{_esc(smart_code)}</code></li>
                        <li><strong>Acción Tomada:</strong> Caso encolado automáticamente en Redis centralizado para reintento.</li>
                        <li><strong>Detalle del Error:</strong> <pre style="background: #f4f4f4; padding: 10px; border-radius: 4px;">{_esc(error_msg)}</pre></li>
                    </ul>
                    <p style="font-size: 12px; color: #777;">Mensaje automático de alerta. Use el Correlation ID para rastrear los logs en CloudWatch.</p>
                </div>
            </body>
        </html>
        """

        cls._programar_envio_background(
            destinatarios=cls._obtener_destinatarios(),
            asunto=asunto,
            cuerpo_html=cuerpo_html,
            clave_dedup=f"falla_infraestructura:{categoria}"
        )

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
        if not settings.ALERT_EMAILS_ENABLED:
            return

        cid = correlation_id or get_correlation_id() or "N/A"
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
                            <td style="padding: 8px; border: 1px solid #ddd;"><strong style="color: #0275d8;"><code>{_esc(cid)}</code></strong></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9;"><strong>Smart Code / Caso:</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;"><code>{_esc(smart_code or 'N/A')}</code></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9;"><strong>Código HTTP:</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;"><code>{_esc(status_code)}</code></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9;"><strong>Campo SFC (sfc_field):</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;"><code>{_esc(sfc_field or 'N/A (Cuerpo General)')}</code></td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd; background: #f9f9f9;"><strong>Respuesta Raw SFC:</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;">
                                <pre style="background: #272822; color: #f8f8f2; padding: 10px; border-radius: 4px; overflow-x: auto;">{_esc(raw_message)}</pre>
                            </td>
                        </tr>
                    </table>

                    <div style="margin-top: 15px; background-color: #eef7ff; padding: 12px; border-left: 4px solid #0275d8;">
                        💡 <strong>Acción recomendada:</strong> Copia el Correlation ID <code>{_esc(cid)}</code> para filtrar la traza en CloudWatch, revisa el payload y agrega la subcadena relevante a <code>errores_sfc.json</code>.
                    </div>
                </div>
            </body>
        </html>
        """

        cls._programar_envio_background(
            destinatarios=destinatario_dev,
            asunto=asunto,
            cuerpo_html=cuerpo_html,
            # Mismo error no mapeado (status_code + campo) no vuelve a disparar
            # correo hasta que pase la ventana, aunque cada despacho lo repita.
            clave_dedup=f"error_no_mapeado:{status_code}:{sfc_field or 'N/A'}"
        )

    @classmethod
    async def notificar_umbral_cola(
        cls, total_pendientes: int, ambiente: str = settings.ENVIRONMENT
    ):
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
                        <li><strong>Ambiente:</strong> {_esc(ambiente.upper())}</li>
                        <li><strong>Casos Pendientes en Cola:</strong> <span style="font-size: 18px; font-weight: bold; color: #d9534f;">{_esc(total_pendientes)}</span></li>
                        <li><strong>Estado SFC:</strong> Intermitente / Caída.</li>
                    </ul>
                    <p>El worker automático continuará intentando el despacho en segundo plano.</p>
                </div>
            </body>
        </html>
        """

        cls._programar_envio_background(
            destinatarios=cls._obtener_destinatarios(),
            asunto=asunto,
            cuerpo_html=cuerpo_html
        )

    @classmethod
    async def notificar_casos_vencimiento_sla(
        cls, casos_vencidos: List[Dict[str, Any]], ambiente: str = settings.ENVIRONMENT
    ):
        if not settings.ALERT_EMAILS_ENABLED or not casos_vencidos:
            return

        total = len(casos_vencidos)
        asunto = f"⏳ [ALERTA SLA] {total} Caso(s) con > 12h en Cola de Contingencia [{ambiente.upper()}]"

        filas_tabla = ""
        for c in casos_vencidos:
            smart_code = _esc(c.get("smart_code", "N/A"))
            cid_caso = _esc(c.get("correlation_id", "N/A"))
            fecha_encolado = _esc(c.get("fecha_encolado", "N/A"))
            horas_cola = _esc(f"{c.get('horas_en_cola', 0):.1f}")
            reintentos = _esc(c.get("reintentos", 0))
            ultimo_error = _esc(str(c.get("ultimo_error", "Sin detalle"))[:100])

            filas_tabla += f"""
            <tr>
                <td style="padding: 8px; border: 1px solid #ddd;"><code>{smart_code}</code></td>
                <td style="padding: 8px; border: 1px solid #ddd;"><code style="color: #0275d8;">{cid_caso}</code></td>
                <td style="padding: 8px; border: 1px solid #ddd;">{fecha_encolado}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align: center; font-weight: bold; color: #d9534f;">{horas_cola} hrs</td>
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
                    <p>Se han identificado <strong>{_esc(total)} caso(s)</strong> que superan las 12 horas de retención en la cola Redis.</p>
                    
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
                </div>
            </body>
        </html>
        """

        cls._programar_envio_background(
            destinatarios=cls._obtener_destinatarios(),
            asunto=asunto,
            cuerpo_html=cuerpo_html
        )

    @classmethod
    async def notificar_caso_fallido_definitivo(
        cls,
        smart_code: str,
        total_intentos: int,
        ultimo_error: str,
        correlation_id: Optional[str] = None,
        ambiente: str = settings.ENVIRONMENT
    ):
        if not settings.ALERT_EMAILS_ENABLED:
            return

        cid = correlation_id or get_correlation_id() or "N/A"
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
                        <li><strong>Ambiente:</strong> {_esc(ambiente.upper())}</li>
                        <li><strong>Correlation ID (CID):</strong> <strong style="color: #0275d8;"><code>{_esc(cid)}</code></strong></li>
                        <li><strong>Smart Code:</strong> <code>{_esc(smart_code)}</code></li>
                        <li><strong>Total Intentos Agotados:</strong> {_esc(total_intentos)}</li>
                        <li><strong>Último Error Registrado:</strong> <pre style="background: #f4f4f4; padding: 10px; border-radius: 4px;">{_esc(ultimo_error)}</pre></li>
                    </ul>
                    <p style="color: #d9534f; font-weight: bold;">⚠️ El Scheduler dejará de reintentar este caso automáticamente.</p>
                </div>
            </body>
        </html>
        """

        cls._programar_envio_background(
            destinatarios=cls._obtener_destinatarios(),
            asunto=asunto,
            cuerpo_html=cuerpo_html
        )

    @classmethod
    async def notificar_recuperacion_sfc(
        cls, total_despachados: int, ambiente: str = settings.ENVIRONMENT
    ):
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
                        <li><strong>Ambiente:</strong> {_esc(ambiente.upper())}</li>
                        <li><strong>Casos Transmitidos Exitosamente:</strong> <strong style="color: #5cb85c; font-size: 16px;">{_esc(total_despachados)}</strong></li>
                        <li><strong>Estado Actual de la Cola:</strong> Vacía / Operación Normal.</li>
                    </ul>
                </div>
            </body>
        </html>
        """

        cls._programar_envio_background(
            destinatarios=cls._obtener_destinatarios(),
            asunto=asunto,
            cuerpo_html=cuerpo_html
        )
        
    # app/services/email_service.py (fragmento adicionado en EmailAlertService)

    # =========================================================================
    # ⚠️ 7. NOTIFICACIÓN DE OBSOLESCENCIA DE CATÁLOGOS / MATRIZ (STALE CACHE)
    # =========================================================================
    @classmethod
    async def notificar_catalogo_stale(
        cls,
        nombre_componente: str,
        edad_horas: float,
        error_msg: str,
        ambiente: str = settings.ENVIRONMENT
    ):
        """Notifica cuando la sincronización con Google Sheets falla y la caché supera el umbral máximo (24h)."""
        if not settings.ALERT_EMAILS_ENABLED:
            return

        asunto = f"⚠️ [ALERTA CATÁLOGO STALE] {nombre_componente} supera umbral de antigüedad ({edad_horas:.1f}h) [{ambiente.upper()}]"

        cuerpo_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <div style="background-color: #d9534f; color: white; padding: 15px; border-radius: 5px;">
                    <h2 style="margin:0;">⚠️ Alerta de Catálogo / Matriz Obsoleta (Stale Cache)</h2>
                </div>
                <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px;">
                    <p>El componente <strong>{_esc(nombre_componente)}</strong> no ha podido refrescar sus datos desde Google Sheets y opera con caché obsoleta.</p>
                    <ul>
                        <li><strong>Ambiente:</strong> {_esc(ambiente.upper())}</li>
                        <li><strong>Componente:</strong> {_esc(nombre_componente)}</li>
                        <li><strong>Antigüedad de la Caché:</strong> <strong style="color: #d9534f;">{_esc(f"{edad_horas:.1f}")} horas</strong></li>
                        <li><strong>Detalle del Error de Refresco:</strong> <pre style="background: #f4f4f4; padding: 10px; border-radius: 4px;">{_esc(error_msg)}</pre></li>
                    </ul>
                    <p style="font-size: 12px; color: #777;">Notificación de control de frescura de catálogos normativos.</p>
                </div>
            </body>
        </html>
        """

        cls._programar_envio_background(
            destinatarios=cls._obtener_destinatarios(),
            asunto=asunto,
            cuerpo_html=cuerpo_html
        )