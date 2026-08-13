import asyncio
import logging
import signal
import sys
import time
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db.redis import init_redis, close_redis, get_redis_client
from app.workers.scheduler import iniciar_scheduler, detener_scheduler
from app.core.exceptions import SfcErrorTranslator
from app.core.mapping import SfcSalesforceMapper
from app.services.crm_webhook_service import close_crm_webhook_client
from app.api.dependencies import _auth_manager_instance

setup_logging()
logger = logging.getLogger("worker_process")

HEARTBEAT_FILE = "/tmp/worker_heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 30  # Intervalo de verificación


def _touch_heartbeat():
    """Escribe el timestamp actual para que el HEALTHCHECK del contenedor
    pueda verificar que el event loop del worker sigue vivo y operativo."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        logger.warning(f"No se pudo escribir el heartbeat file: {e}")


async def _check_redis_health() -> bool:
    """
    🟢 FIX HALLAZGO 38: Prueba activa de conectividad y operación real con Redis.
    Ejecuta un comando PING contra el cliente de Redis centralizado.
    """
    try:
        redis = get_redis_client()
        if not redis:
            logger.error("❌ [Worker Healthcheck] Cliente Redis es None.")
            return False
        
        # Ejecuta PING directo con timeout corto
        res = await asyncio.wait_for(redis.ping(), timeout=5.0)
        return res is True or str(res).upper() == "PONG" or res == b"PONG"
    except Exception as e:
        logger.error(f"❌ [Worker Healthcheck] Fallo de conectividad/operación con Redis: {e}")
        return False


async def _heartbeat_loop(stop_event: asyncio.Event):
    """
    🟢 FIX HALLAZGO 38: Tarea asíncrona en segundo plano que valida Redis
    antes de actualizar la frescura del archivo de heartbeat.
    """
    while not stop_event.is_set():
        is_redis_ok = await _check_redis_health()
        
        if is_redis_ok:
            _touch_heartbeat()
            logger.debug("💚 [Worker Heartbeat] Heartbeat actualizado exitosamente (Redis OK).")
        else:
            logger.warning(
                "⚠️ [Worker Heartbeat] Redis inalcanzable o degradado. "
                "Omitiendo actualización de heartbeat para que ECS detecte la degradación."
            )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run_worker_process():
    logger.info(f"⚙️ Iniciando Worker Proceso de Fondo para {settings.PROJECT_NAME} [{settings.ENVIRONMENT}]")

    # 1. Conectar Redis
    await init_redis()

    # 2. Precargar catálogos y matriz de errores
    try:
        await SfcErrorTranslator.obtener_matriz_errores()
        await SfcSalesforceMapper.obtener_catalogos_y_mapeos()
    except Exception as e:
        logger.error(f"Error precargando matriz/catálogos en Worker: {e}")

    # 3. Forzar e Iniciar Scheduler
    settings.RUN_SCHEDULER = True
    iniciar_scheduler()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop_handler():
        logger.info("🛑 Recibida señal de apagado (SIGTERM/SIGINT). Iniciando cierre seguro...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop_handler)
        except (NotImplementedError, AttributeError):
            pass

    # Primera verificación de arranque
    if await _check_redis_health():
        _touch_heartbeat()

    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event))
    logger.info("🟢 Worker activo y escuchando eventos/reintentos de la cola Redis...")

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Interrupción por teclado/sistema recibida.")
    finally:
        logger.info("🧹 Ejecutando limpieza y cierre de recursos del Worker...")
        stop_event.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        detener_scheduler()
        await close_redis()
        await close_crm_webhook_client()
        await _auth_manager_instance.close()
        logger.info("👋 Worker detenido completamente de forma segura.")


if __name__ == "__main__":
    asyncio.run(run_worker_process())