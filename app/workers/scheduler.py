import uuid
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db.redis import get_redis_client
from app.services.queue_service import QueueService
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.services.email_service import EmailAlertService
from app.api.dependencies import get_sfc_client, get_s3_client
from app.services.crm_webhook_service import CrmWebhookService
from app.core.config import settings
from app.core.middleware import correlation_id_ctx  # 👈 Importación de la ContextVar de CID

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def reintentar_despachos_pendientes_job():
    """
    Job que consume los pendientes de Redis, reintenta la comunicación con la SFC,
    audita el tiempo de retención (SLA) y notifica la autorrecuperación cuando la cola se vacía.
    Utiliza un Lock Distribuido para ejecuciones multicontenedor seguras.
    Preserva y propaga el Correlation ID (CID) original de cada caso.
    """
    redis = get_redis_client()
    if not redis:
        logger.warning("⚠️ [Scheduler Job] Cliente Redis no disponible. Omitiendo ciclo de reintentos.")
        return

    # 🔒 1. CERROJO DISTRIBUIDO (Lock con expiración automática de 55s)
    LOCK_KEY = "sfc:queue:lock:retry_job"
    acquired = await redis.set(LOCK_KEY, "locked", nx=True, px=55000)

    if not acquired:
        logger.info("🔒 [Scheduler Job] Cerrojo activo en otra réplica/contenedor. Omitiendo ejecución en este nodo.")
        return

    try:
        queue_service = QueueService(redis)

        # ⏳ 2. Control de SLA: Evaluar e informar casos con > 12 horas retenidos
        try:
            casos_vencidos = await queue_service.obtener_casos_vencidos_sla(horas_limite=12)
            if casos_vencidos:
                logger.warning(f"⏳ [Scheduler Job] {len(casos_vencidos)} caso(s) superan las 12h en la cola.")
                await EmailAlertService.notificar_casos_vencimiento_sla(casos_vencidos=casos_vencidos)
        except Exception as e:
            logger.error(f"❌ [Scheduler Job] Error al verificar SLA de la cola: {str(e)}")

        # 🔄 3. Obtener los registros pendientes cuya fecha de reintento ya venció
        pendientes = await queue_service.obtener_pendientes_para_reintento()

        if not pendientes:
            return

        logger.info(f"🔄 [Scheduler Job] Se encontraron {len(pendientes)} despachos pendientes en Redis. Iniciando reintentos...")

        sfc_client = get_sfc_client()
        s3_client = get_s3_client()
        orquestador = DespachoQuejaOrquestador(sfc_client=sfc_client, s3_client=s3_client)

        casos_despachados_exito = 0

        for item in pendientes:
            # 🔑 Extraer el CID guardado en Redis o generar uno de respaldo
            item_data = item.to_dict() if hasattr(item, "to_dict") else {}
            cid_guardado = (
                item_data.get("correlation_id")
                or item.payload_json.get("correlation_id")
                or str(uuid.uuid4())
            )

            # 📌 Rehidratar el ContextVar para el hilo asíncrono actual
            token = correlation_id_ctx.set(cid_guardado)

            try:
                resultado = await orquestador.procesar_despacho_raw_json(item.payload_json)

                if resultado.get("status") != "error":
                    await queue_service.marcar_exitoso(item.id)
                    casos_despachados_exito += 1
                    
                    # Envío asíncrono al CRM para notificar creación exitosa en la SFC
                    case_id_crm = item.payload_json.get("Case_id") or item.smart_code
                    await CrmWebhookService.notificar_creacion_exitosa(
                        case_id_crm=case_id_crm,
                        smart_code=item.smart_code
                    )
                    
                    logger.info(f"✅ [Scheduler Job] Caso {item.smart_code} entregado exitosamente a la SFC desde Redis.")
                else:
                    await queue_service.registrar_fallo(item.id, error_msg=resultado.get("message"))

            except Exception as exc:
                logger.warning(f"⚠️ [Scheduler Job] Reintento fallido para el caso {item.smart_code}: {str(exc)}")
                await queue_service.registrar_fallo(item.id, error_msg=str(exc))
            finally:
                # 🧹 Limpiar la variable de contexto al finalizar el procesamiento del ítem
                correlation_id_ctx.reset(token)

        # ✅ 4. Notificación de Autorrecuperación
        try:
            totales_restantes = await queue_service.contar_pendientes()
            if casos_despachados_exito > 0 and totales_restantes == 0:
                logger.info(
                    f"✅ [Scheduler Job] Cola de Redis completamente vaciada ({casos_despachados_exito} entregados). "
                    f"Enviando notificación de autorrecuperación..."
                )
                await EmailAlertService.notificar_recuperacion_sfc(total_despachados=casos_despachados_exito)
        except Exception as e:
            logger.error(f"❌ [Scheduler Job] Error al verificar estado de autorrecuperación: {str(e)}")

    finally:
        # Liberación limpia del lock
        await redis.delete(LOCK_KEY)


async def purgar_cola_job():
    """Job diario que elimina registros 'EXITOSO' antiguos de la cola Redis."""
    redis = get_redis_client()
    if redis:
        queue_service = QueueService(redis)
        await queue_service.purgar_registros_antiguos(dias_retencion=settings.QUEUE_RETENTION_DAYS)


def iniciar_scheduler():
    """Inicializa los trabajos programados de APScheduler si la cola está habilitada."""
    if settings.QUEUE_ENABLED and not scheduler.running:
        scheduler.add_job(
            reintentar_despachos_pendientes_job,
            trigger="interval",
            minutes=settings.QUEUE_RETRY_INTERVAL_MINUTES,
            id="sfc_queue_retry_job",
            replace_existing=True,
            max_instances=1,
            coalesce=True
        )

        scheduler.add_job(
            purgar_cola_job,
            trigger="cron",
            hour=0,
            minute=0,
            id="sfc_queue_purge_job",
            replace_existing=True,
            max_instances=1
        )

        scheduler.start()
        logger.info(
            f"🚀 APScheduler corriendo reintentos sobre Redis cada {settings.QUEUE_RETRY_INTERVAL_MINUTES}m "
            f"y purga nocturna a las 00:00 UTC."
        )


def detener_scheduler():
    """Detiene formalmente APScheduler al apagar el microservicio."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("🛑 APScheduler detenido.")