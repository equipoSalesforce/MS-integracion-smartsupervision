# app/workers/scheduler.py
import asyncio
import uuid
import logging
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db.redis import get_redis_client
from app.services.queue_service import QueueService
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.services.email_service import EmailAlertService
from app.services.idempotency_service import IdempotencyService
from app.api.dependencies import get_sfc_client, get_s3_client
from app.services.crm_webhook_service import CrmWebhookService
from app.core.config import settings
from app.core.middleware import correlation_id_ctx

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

# 🔑 Hash Tag unificado para candados del Scheduler (compatible con Redis Cluster)
SCHEDULER_LOCK_PREFIX = "{sfc:scheduler}"


class QueueLockWatchdog:
    """
    Context Manager asíncrono que mantiene vivo el arrendamiento de un ítem en Redis
    renovando periódicamente el TTL mientras la tarea principal esté en ejecución.
    """
    def __init__(
        self, 
        queue_service: QueueService, 
        registro_id: int, 
        worker_id: str, 
        lease_segundos: int = 60, 
        intervalo_segundos: int = 15
    ):
        self.queue_service = queue_service
        self.registro_id = registro_id
        self.worker_id = worker_id
        self.lease_segundos = lease_segundos
        self.intervalo_segundos = intervalo_segundos
        self._task: Optional[asyncio.Task] = None

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self.intervalo_segundos)
            exito = await self.queue_service.extender_lease_item(
                registro_id=self.registro_id,
                worker_id=self.worker_id,
                lease_segundos=self.lease_segundos
            )
            if not exito:
                logger.warning(
                    f"⚠️ [Lock Watchdog] No se pudo extender el lease para el registro {self.registro_id}. "
                    f"El bloqueo expiró o fue tomado por otro worker."
                )
                break

    async def __aenter__(self):
        self._task = asyncio.create_task(self._heartbeat())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def _es_falla_infraestructura(error_msg: Optional[str]) -> bool:
    """Evalúa si un mensaje corresponde a una indisponibilidad/caída de la SFC o red."""
    if not error_msg:
        return False
    msg_lower = error_msg.lower()
    keywords = [
        "resource_exhausted", "quota exceeded", "503", "502", "504", "429",
        "connecterror", "connect error", "timeout", "connection refused",
        "indisponible", "no se encuentra disponible", "throttled", "throttling",
        "name or service not known", "service unavailable", "bad gateway"
    ]
    return any(kw in msg_lower for kw in keywords)


async def reintentar_despachos_pendientes_job():
    redis = get_redis_client()
    if not redis:
        logger.warning("⚠️ [Scheduler Job] Cliente Redis no disponible. Omitiendo ciclo de reintentos.")
        return

    # 🟢 FIX MULTI-INSTANCIA: Candado distribuido global (ex=50s) para que solo UN worker ejecute el ciclo
    lock_key = f"{SCHEDULER_LOCK_PREFIX}:lock:retry_job"
    lock_acquired = await redis.set(lock_key, "locked", nx=True, ex=50)
    if not lock_acquired:
        logger.debug("ℹ️ [Scheduler Job] Otro nodo worker ya se encuentra ejecutando el ciclo de reintentos.")
        return

    worker_id = f"worker_node:{uuid.uuid4()}"
    queue_service = QueueService(redis)
    idempotency_service = IdempotencyService(redis)

    # 1. Control de SLA
    try:
        casos_vencidos = await queue_service.obtener_casos_vencidos_sla(horas_limite=12)
        if casos_vencidos:
            logger.warning(f"⏳ [Scheduler Job] {len(casos_vencidos)} caso(s) superan las 12h en la cola.")
            await EmailAlertService.notificar_casos_vencimiento_sla(casos_vencidos=casos_vencidos)
    except Exception as e:
        logger.error(f"❌ [Scheduler Job] Error al verificar SLA de la cola: {str(e)}")

    # 2. Obtener registros pendientes vencidos
    pendientes = await queue_service.obtener_pendientes_para_reintento()
    if not pendientes:
        return

    sfc_client = get_sfc_client()
    s3_client = get_s3_client()
    orquestador = DespachoQuejaOrquestador(sfc_client=sfc_client, s3_client=s3_client)

    casos_despachados_exito = 0

    for index, item in enumerate(pendientes):
        reclamado = await queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id,
            worker_id=worker_id,
            lease_segundos=60
        )

        if not reclamado:
            continue

        item_data = item.to_dict() if hasattr(item, "to_dict") else {}
        cid_guardado = (
            item_data.get("correlation_id")
            or item.payload_json.get("correlation_id")
            or str(uuid.uuid4())
        )

        token = correlation_id_ctx.set(cid_guardado)
        es_falla_infraestructura = False

        async with QueueLockWatchdog(
            queue_service=queue_service, 
            registro_id=item.id, 
            worker_id=worker_id, 
            lease_segundos=60, 
            intervalo_segundos=15
        ):
            try:
                payload_actual = item.payload_json
                sfc_ya_completado = payload_actual.get("_sfc_completado", False)
                resultado = payload_actual.get("_sfc_resultado", {})

                # PASO 1: Procesamiento en SFC (solo si no fue completado previamente)
                if not sfc_ya_completado:
                    resultado = await orquestador.procesar_despacho_raw_json(payload_actual)

                    if resultado.get("status") == "error":
                        error_msg = resultado.get("message") or "Error en el despacho a la SFC"
                        await queue_service.registrar_fallo(item.id, error_msg=error_msg)
                        es_falla_infraestructura = _es_falla_infraestructura(error_msg)
                        continue

                    await idempotency_service.registrar_exito(
                        smart_code=item.smart_code,
                        payload_dict=payload_actual,
                        sfc_response=resultado
                    )
                    payload_actual["_sfc_completado"] = True
                    payload_actual["_sfc_resultado"] = resultado

                # PASO 2: Notificación al CRM Webhook
                case_id_crm = payload_actual.get("Case_id") or item.smart_code
                crm_notificado = await CrmWebhookService.notificar_resolucion_contingencia(
                    case_id_crm=case_id_crm,
                    smart_code=item.smart_code
                )

                if not crm_notificado:
                    error_msg = (
                        f"SFC procesó la queja exitosamente ({resultado.get('message', '')}), "
                        f"pero la notificación hacia el CRM Webhook falló."
                    )
                    logger.warning(f"⚠️ [Scheduler Job] {error_msg}")
                    await queue_service.registrar_fallo(item.id, error_msg=error_msg)
                else:
                    await queue_service.marcar_exitoso(item.id)
                    casos_despachados_exito += 1
                    logger.info(
                        f"✅ [Scheduler Job] Caso {item.smart_code} entregado exitosamente a la SFC "
                        f"y confirmado al CRM desde Redis."
                    )

            except Exception as exc:
                error_msg = str(exc)
                logger.warning(f"⚠️ [Scheduler Job] Reintento fallido para el caso {item.smart_code}: {error_msg}")
                await queue_service.registrar_fallo(item.id, error_msg=error_msg)
                es_falla_infraestructura = _es_falla_infraestructura(error_msg)
            finally:
                correlation_id_ctx.reset(token)

        if es_falla_infraestructura:
            casos_restantes = pendientes[index + 1:]
            if casos_restantes:
                ids_restantes = [r.id for r in casos_restantes]
                await queue_service.diferir_pendientes_por_caida_sfc(
                    registro_ids=ids_restantes,
                    minutos_delay=settings.QUEUE_RETRY_INTERVAL_MINUTES
                )
            break

    # 3. Notificación de Autorrecuperación
    try:
        totales_restantes = await queue_service.contar_pendientes()
        if casos_despachados_exito > 0 and totales_restantes == 0:
            await EmailAlertService.notificar_recuperacion_sfc(total_despachados=casos_despachados_exito)
    except Exception as e:
        logger.error(f"❌ [Scheduler Job] Error al verificar estado de autorrecuperación: {str(e)}")


async def purgar_cola_job():
    """Job diario que elimina registros 'EXITOSO' antiguos de la cola Redis."""
    redis = get_redis_client()
    if not redis:
        return

    # 🟢 FIX MULTI-INSTANCIA: Candado distribuido diario para evitar ejecución duplicada de purga
    lock_key = f"{SCHEDULER_LOCK_PREFIX}:lock:purge_job"
    lock_acquired = await redis.set(lock_key, "locked", nx=True, ex=3600)
    if not lock_acquired:
        logger.debug("ℹ️ [Scheduler Job] Otro nodo worker ya se encuentra ejecutando la purga nocturna.")
        return

    queue_service = QueueService(redis)
    await queue_service.purgar_registros_antiguos(
        dias_retencion=settings.QUEUE_RETENTION_DAYS,
        dias_retencion_dlq=settings.QUEUE_RETENTION_DAYS_DLQ
    )


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