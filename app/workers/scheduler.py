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
from app.core.metrics import emit_emf_metric

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

SCHEDULER_LOCK_PREFIX = "{sfc:scheduler}"

RELEASE_LOCK_LUA_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

EXTEND_LOCK_LUA_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], tonumber(ARGV[2]))
else
    return 0
end
"""


class SchedulerJobLock:
    def __init__(
        self, 
        redis_client, 
        lock_key: str, 
        lease_segundos: int = 60, 
        intervalo_heartbeat: int = 15
    ):
        self.redis = redis_client
        self.lock_key = lock_key
        self.lease_segundos = lease_segundos
        self.intervalo_heartbeat = intervalo_heartbeat
        self.owner_token = str(uuid.uuid4())
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.acquired = False

    async def acquire(self) -> bool:
        if not self.redis:
            return False
        try:
            res = await self.redis.set(
                self.lock_key, 
                self.owner_token, 
                nx=True, 
                ex=self.lease_segundos
            )
            self.acquired = bool(res)
            if self.acquired:
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                logger.debug(f"🔑 [Scheduler Lock] Candado '{self.lock_key}' adquirido por worker token: {self.owner_token}")
            return self.acquired
        except Exception as e:
            logger.error(f"❌ Error al adquirir lock distribuido '{self.lock_key}': {e}")
            return False

    async def _heartbeat_loop(self):
        lease_ms = str(self.lease_segundos * 1000)
        while self.acquired:
            await asyncio.sleep(self.intervalo_heartbeat)
            try:
                res = await self.redis.eval(
                    EXTEND_LOCK_LUA_SCRIPT,
                    1,
                    self.lock_key,
                    self.owner_token,
                    lease_ms
                )
                if res != 1:
                    logger.warning(
                        f"⚠️ [Scheduler Lock] No se pudo extender el lock '{self.lock_key}'. "
                        f"El candado expiró o pertenece a otro worker."
                    )
                    break
                logger.debug(f"🔄 [Scheduler Lock] Heartbeat: Lock '{self.lock_key}' renovado exitosamente.")
            except Exception as e:
                logger.error(f"❌ Error renovando lock '{self.lock_key}': {e}")

    async def release(self):
        if not self.acquired:
            return
        self.acquired = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self.redis:
            try:
                await self.redis.eval(
                    RELEASE_LOCK_LUA_SCRIPT,
                    1,
                    self.lock_key,
                    self.owner_token
                )
                logger.debug(f"🔓 [Scheduler Lock] Lock '{self.lock_key}' liberado limpiamente (CAD).")
            except Exception as e:
                logger.error(f"❌ Error al liberar lock '{self.lock_key}': {e}")

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


class QueueLockWatchdog:
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


async def _emitir_metricas_cola(
    queue_service: QueueService, casos_exito: int, casos_fallidos: int
) -> None:
    """
    Observabilidad: emite métricas EMF (CloudWatch Embedded Metric Format) del
    estado de la cola al final de cada ciclo del scheduler. Best-effort: un fallo
    aquí nunca debe impedir liberar el lock del job ni afectar el resultado del
    ciclo, por eso vive aislado en su propio try/except.
    """
    try:
        queue_depth = await queue_service.contar_pendientes()
        oldest_age = await queue_service.obtener_edad_item_mas_antiguo_pendiente()

        metrics = {
            "queue_depth": (queue_depth, "Count"),
            "dispatch_success": (casos_exito, "Count"),
            "dispatch_failure": (casos_fallidos, "Count"),
        }
        if oldest_age is not None:
            metrics["oldest_pending_age_seconds"] = (oldest_age, "Seconds")

        emit_emf_metric(namespace="SSV/Queue", metrics=metrics)
    except Exception as e:
        logger.warning(f"⚠️ [Scheduler Job] No se pudieron emitir métricas de la cola: {e}")


async def reintentar_despachos_pendientes_job():
    # 🟡 P1-02 (aceptado, no se corrige): el lock es global por diseño — un solo
    # nodo procesa el ciclo de reintentos a la vez, aunque haya varias réplicas.
    # Es la forma más simple de evitar doble despacho a la SFC entre réplicas; el
    # claim por-item (P0-04/P0-05) ya permite que ese nodo procese muchos items en
    # un solo ciclo, así que esto es un techo de throughput, no un bug de
    # correctitud. Si el volumen de reintentos lo exige, la vía de escalar sería
    # particionar la cola (p.ej. registro_id % N) para que cada réplica tome su
    # propio lock por partición.
    redis = get_redis_client()
    if not redis:
        logger.warning("⚠️ [Scheduler Job] Cliente Redis no disponible. Omitiendo ciclo de reintentos.")
        return

    lock_key = f"{SCHEDULER_LOCK_PREFIX}:lock:retry_job"
    job_lock = SchedulerJobLock(
        redis_client=redis,
        lock_key=lock_key,
        lease_segundos=60,
        intervalo_heartbeat=15
    )

    if not await job_lock.acquire():
        logger.debug("ℹ️ [Scheduler Job] Otro nodo worker ya se encuentra ejecutando el ciclo de reintentos.")
        return

    # 🟢 Observabilidad: se define fuera del try para que el finally siempre pueda
    # emitir métricas (queue_depth, etc.) incluso si el ciclo corta temprano.
    queue_service = QueueService(redis)
    casos_despachados_exito = 0
    casos_fallidos = 0

    try:
        worker_id = f"worker_node:{uuid.uuid4()}"
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
        # 🟢 FIX P1-15: obtener_pendientes_para_reintento ya no traga excepciones de
        # Redis devolviendo []; se distingue explícitamente "no hay nada pendiente" de
        # "no se pudo verificar" para no ocultar una caída de Redis en este ciclo.
        try:
            pendientes = await queue_service.obtener_pendientes_para_reintento()
        except Exception as e:
            logger.critical(f"🔥 [Scheduler Job] No se pudo consultar la cola de pendientes en Redis: {e}")
            return

        if not pendientes:
            return

        sfc_client = get_sfc_client()
        s3_client = get_s3_client()
        orquestador = DespachoQuejaOrquestador(sfc_client=sfc_client, s3_client=s3_client)

        for index, item in enumerate(pendientes):
            # 🟢 FIX P0-04: el claim ahora devuelve el item TAL COMO ESTÁ en Redis en ese
            # instante (no la copia leída durante el listado previo), cerrando la ventana
            # en la que el payload pudo haber sido sobrescrito por un evento más nuevo del
            # mismo smart_code entre `obtener_pendientes_para_reintento` y el claim.
            item_reclamado = await queue_service.reclamar_item_para_procesamiento(
                registro_id=item.id,
                worker_id=worker_id,
                lease_segundos=60
            )

            if item_reclamado is None:
                continue

            item = item_reclamado
            item_data = item.to_dict()

            # 🟢 FIX HALLAZGO 47: Extraer el correlation_id preservado del modelo ColaItemRedis
            cid_guardado = (
                item.correlation_id
                or item_data.get("correlation_id")
                or item.payload_json.get("correlation_id")
                or str(uuid.uuid4())
            )

            # Inyectar el correlation_id original al ContextVar para que logs y HTTP client lo utilicen
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
                    sfc_ya_completado = item.sfc_completado or payload_actual.get("_sfc_completado", False)
                    resultado = item.sfc_response or payload_actual.get("_sfc_resultado", {})

                    # PASO 1: Procesamiento en SFC (solo si no fue completado previamente)
                    if not sfc_ya_completado:
                        resultado = await orquestador.procesar_despacho_raw_json(payload_actual)

                        if resultado.get("status") == "error":
                            error_msg = resultado.get("message") or "Error en el despacho a la SFC"
                            await queue_service.registrar_fallo(item.id, error_msg=error_msg)
                            casos_fallidos += 1
                            es_falla_infraestructura = _es_falla_infraestructura(error_msg)
                            continue

                        # 🟢 FIX P0-06: SFC ya recibió y procesó el envío en este punto. Si la
                        # persistencia durable de ese hecho falla (tras los reintentos internos de
                        # cada método), NO se debe tratar como un fallo normal de la operación —
                        # registrar_fallo incrementaría intentos y el próximo retry reenviaría a
                        # SFC. Se aísla en su propio try/except: se alerta como falla crítica de
                        # infraestructura y se deja el item intacto para reintentar sólo la
                        # persistencia en el próximo ciclo, en vez de silenciar el fallo.
                        try:
                            await idempotency_service.registrar_exito(
                                smart_code=item.smart_code,
                                payload_dict=payload_actual,
                                sfc_response=resultado
                            )
                            await queue_service.marcar_sfc_completado(item.id, sfc_response=resultado)
                        except Exception as persist_err:
                            logger.critical(
                                f"🔥 [Scheduler Job] SFC procesó exitosamente el caso {item.smart_code} pero no fue "
                                f"posible persistir el estado durable (idempotencia/SFC_DONE): {persist_err}. "
                                f"Riesgo de reenvío duplicado a la SFC en el próximo reintento."
                            )
                            await EmailAlertService.notificar_falla_infraestructura(
                                smart_code=item.smart_code,
                                error_msg=f"Persistencia post-SFC fallida (riesgo de duplicado): {persist_err}"
                            )
                            continue
                        sfc_ya_completado = True

                    # PASO 2: Notificación al CRM Webhook (utiliza automáticamente get_correlation_id())
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
                        casos_fallidos += 1
                    else:
                        try:
                            # 🟢 FIX P0-04/P0-05: se pasa worker_id + la versión reclamada para que
                            # MARK_SUCCESS verifique ownership y que el registro no fue sobrescrito.
                            resultado_mark = await queue_service.marcar_exitoso(
                                item.id, worker_id=worker_id, expected_version=item.version
                            )
                            if resultado_mark == "completed":
                                casos_despachados_exito += 1
                                logger.info(
                                    f"✅ [Scheduler Job] Caso {item.smart_code} entregado exitosamente a la SFC "
                                    f"y confirmado al CRM desde Redis [CID: {cid_guardado}]."
                                )
                            else:
                                # "not_owner"/"version_mismatch"/"not_found": el envío a SFC/CRM sí
                                # ocurrió, pero este worker ya no tiene autoridad sobre el registro.
                                # No es un fallo de reintentos: no se llama a registrar_fallo.
                                logger.warning(
                                    f"⚠️ [Scheduler Job] Caso {item.smart_code} procesado en SFC/CRM pero no "
                                    f"completado en Redis (motivo: {resultado_mark}). Ver contenido vigente."
                                )
                        except Exception as redis_err:
                            logger.critical(
                                f"🔥 [Scheduler Job] ERROR CRÍTICO DE PERSISTENCIA: Caso {item.smart_code} (ID: {item.id}) "
                                f"se procesó en SFC y CRM, pero falló la actualización en Redis: {redis_err}"
                            )

                except Exception as exc:
                    error_msg = str(exc)
                    logger.warning(f"⚠️ [Scheduler Job] Reintento fallido para el caso {item.smart_code}: {error_msg}")
                    await queue_service.registrar_fallo(item.id, error_msg=error_msg)
                    casos_fallidos += 1
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

    finally:
        await _emitir_metricas_cola(queue_service, casos_despachados_exito, casos_fallidos)
        await job_lock.release()


async def purgar_cola_job():
    redis = get_redis_client()
    if not redis:
        return

    lock_key = f"{SCHEDULER_LOCK_PREFIX}:lock:purge_job"
    job_lock = SchedulerJobLock(
        redis_client=redis,
        lock_key=lock_key,
        lease_segundos=300,
        intervalo_heartbeat=30
    )

    if not await job_lock.acquire():
        logger.debug("ℹ️ [Scheduler Job] Otro nodo worker ya se encuentra ejecutando la purga nocturna.")
        return

    try:
        queue_service = QueueService(redis)
        await queue_service.purgar_registros_antiguos(
            dias_retencion=settings.QUEUE_RETENTION_DAYS,
            dias_retencion_dlq=settings.QUEUE_RETENTION_DAYS_DLQ
        )
    finally:
        await job_lock.release()


def iniciar_scheduler():
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
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("🛑 APScheduler detenido.")