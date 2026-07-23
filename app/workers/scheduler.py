import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db.database import AsyncSessionLocal
from app.services.queue_service import QueueService
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.services.email_service import EmailAlertService
from app.api.dependencies import get_sfc_client, get_s3_client
from app.core.config import settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def reintentar_despachos_pendientes_job():
    """Job que consume los pendientes de SQLite, reintenta la comunicación con la SFC,
    audita el tiempo de retención (SLA) y notifica la autorrecuperación cuando la cola se vacía.
    """
    async with AsyncSessionLocal() as session:
        queue_service = QueueService(session)

        # ⏳ 1. Control de SLA: Evaluar e informar casos con > 12 horas retenidos
        try:
            casos_vencidos = await queue_service.obtener_casos_vencidos_sla(horas_limite=12)
            if casos_vencidos:
                logger.warning(f"⏳ [Scheduler Job] {len(casos_vencidos)} caso(s) superan las 12h en la cola.")
                await EmailAlertService.notificar_casos_vencimiento_sla(casos_vencidos=casos_vencidos)
        except Exception as e:
            logger.error(f"❌ [Scheduler Job] Error al verificar SLA de la cola: {str(e)}")

        # 🔄 2. Obtener los registros pendientes cuya fecha de reintento ya venció
        pendientes = await queue_service.obtener_pendientes_para_reintento()

        if not pendientes:
            return

        logger.info(f"🔄 [Scheduler Job] Se encontraron {len(pendientes)} despachos pendientes en SQLite. Iniciando reintentos...")

        # Instanciamos dependencias base de infraestructura
        sfc_client = get_sfc_client()
        s3_client = get_s3_client()
        orquestador = DespachoQuejaOrquestador(sfc_client=sfc_client, s3_client=s3_client)

        casos_despachados_exito = 0

        for item in pendientes:
            try:
                # Reintento directo usando el JSON crudo almacenado
                resultado = await orquestador.procesar_despacho_raw_json(item.payload_json)

                if resultado.get("status") != "error":
                    await queue_service.marcar_exitoso(item.id)
                    casos_despachados_exito += 1
                    logger.info(f"✅ [Scheduler Job] Caso {item.smart_code} entregado exitosamente a la SFC desde la cola.")
                else:
                    await queue_service.registrar_fallo(item.id, error_msg=resultado.get("message"))

            except Exception as exc:
                logger.warning(f"⚠️ [Scheduler Job] Reintento fallido para el caso {item.smart_code}: {str(exc)}")
                await queue_service.registrar_fallo(item.id, error_msg=str(exc))

        # ✅ 3. Notificación de Autorrecuperación (Si hubo entregas y la cola quedó totalmente vacía)
        try:
            totales_restantes = await queue_service.contar_pendientes()
            if casos_despachados_exito > 0 and totales_restantes == 0:
                logger.info(
                    f"✅ [Scheduler Job] Cola completamente vaciada ({casos_despachados_exito} entregados). "
                    f"Enviando notificación de autorrecuperación..."
                )
                await EmailAlertService.notificar_recuperacion_sfc(total_despachados=casos_despachados_exito)
        except Exception as e:
            logger.error(f"❌ [Scheduler Job] Error al verificar estado de autorrecuperación: {str(e)}")


async def purgar_cola_job():
    """Job diario que elimina registros 'EXITOSO' antiguos de la BD SQLite."""
    async with AsyncSessionLocal() as session:
        queue_service = QueueService(session)
        await queue_service.purgar_registros_antiguos(dias_retencion=settings.QUEUE_RETENTION_DAYS)


def iniciar_scheduler():
    """Inicializa los trabajos programados de APScheduler si la cola está habilitada."""
    if settings.QUEUE_ENABLED and not scheduler.running:
        # 1. Job de Reintentos y SLA (Corre cada N minutos con protección de concurrencia)
        scheduler.add_job(
            reintentar_despachos_pendientes_job,
            trigger="interval",
            minutes=settings.QUEUE_RETRY_INTERVAL_MINUTES,
            id="sfc_queue_retry_job",
            replace_existing=True,
            max_instances=1,
            coalesce=True
        )

        # 2. Job de Purga Nocturna (Corre todos los días a las 00:00 UTC)
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
            f"🚀 APScheduler corriendo reintentos cada {settings.QUEUE_RETRY_INTERVAL_MINUTES}m "
            f"y purga nocturna a las 00:00 UTC."
        )


def detener_scheduler():
    """Detiene formalmente APScheduler al apagar el microservicio."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("🛑 APScheduler detenido.")