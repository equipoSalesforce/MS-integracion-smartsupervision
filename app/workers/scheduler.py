import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.db.database import AsyncSessionLocal
from app.services.queue_service import QueueService
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.api.dependencies import get_sfc_client, get_s3_client
from app.core.config import settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

async def reintentar_despachos_pendientes_job():
    """Job que consume los pendientes de SQLite y reintenta la comunicación con la SFC."""
    async with AsyncSessionLocal() as session:
        queue_service = QueueService(session)
        pendientes = await queue_service.obtener_pendientes_para_reintento()

        if not pendientes:
            return

        logger.info(f"🔄 [Scheduler Job] Se encontraron {len(pendientes)} despachos pendientes en SQLite. Iniciando reintentos...")
        
        # Instanciamos dependencias base
        sfc_client = get_sfc_client()
        s3_client = get_s3_client()
        orquestador = DespachoQuejaOrquestador(sfc_client=sfc_client, s3_client=s3_client)

        for item in pendientes:
            try:
                # Reintento directo usando el JSON crudo almacenado
                resultado = await orquestador.procesar_despacho_raw_json(item.payload_json)
                
                if resultado.get("status") != "error":
                    await queue_service.marcar_exitoso(item.id)
                    logger.info(f"✅ [Scheduler Job] Caso {item.smart_code} entregado exitosamente a la SFC desde la cola.")
                else:
                    await queue_service.registrar_fallo(item.id, error_msg=resultado.get("message"))

            except Exception as exc:
                logger.warning(f"⚠️ [Scheduler Job] Reintento fallido para el caso {item.smart_code}: {str(exc)}")
                await queue_service.registrar_fallo(item.id, error_msg=str(exc))

async def purgar_cola_job():
    """Job diario que elimina registros 'EXITOSO' antiguos de la BD SQLite."""
    async with AsyncSessionLocal() as session:
        queue_service = QueueService(session)
        await queue_service.purgar_registros_antiguos(dias_retencion=settings.QUEUE_RETENTION_DAYS)

def iniciar_scheduler():
    if settings.QUEUE_ENABLED and not scheduler.running:
        scheduler.add_job(
            reintentar_despachos_pendientes_job,
            trigger="interval",
            minutes=settings.QUEUE_RETRY_INTERVAL_MINUTES,
            id="sfc_queue_retry_job",
            replace_existing=True
        )
        scheduler.start()
        logger.info(f"🚀 APScheduler corriendo cada {settings.QUEUE_RETRY_INTERVAL_MINUTES} minutos.")

def detener_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("🛑 APScheduler detenido.")
        
def iniciar_scheduler():
    if settings.QUEUE_ENABLED and not scheduler.running:
        # 1. Job de Reintentos (Corre cada N minutos)
        scheduler.add_job(
            reintentar_despachos_pendientes_job,
            trigger="interval",
            minutes=settings.QUEUE_RETRY_INTERVAL_MINUTES,
            id="sfc_queue_retry_job",
            replace_existing=True
        )
        
        # 2. Job de Purga Nocturna (Corre todos los días a las 00:00 UTC)
        scheduler.add_job(
            purgar_cola_job,
            trigger="cron",
            hour=0,
            minute=0,
            id="sfc_queue_purge_job",
            replace_existing=True
        )
        
        scheduler.start()
        logger.info(f"🚀 APScheduler corriendo reintentos cada {settings.QUEUE_RETRY_INTERVAL_MINUTES}m y purga a las 00:00.")