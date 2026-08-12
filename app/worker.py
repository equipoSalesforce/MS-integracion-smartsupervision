# app/worker.py
import asyncio
import logging
import signal
import sys
import time
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db.redis import init_redis, close_redis
from app.workers.scheduler import iniciar_scheduler, detener_scheduler
from app.core.exceptions import SfcErrorTranslator
from app.core.mapping import SfcSalesforceMapper
from app.services.crm_webhook_service import close_crm_webhook_client
from app.api.dependencies import _auth_manager_instance

setup_logging()
logger = logging.getLogger("worker_process")

HEARTBEAT_FILE = "/tmp/worker_heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 30  # 🟢 Intervalo reducido a 30s para mayor margen en Healthcheck


def _touch_heartbeat():
    """Escribe el timestamp actual para que el HEALTHCHECK del contenedor
    pueda verificar que el event loop del worker sigue vivo."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        logger.warning(f"No se pudo escribir el heartbeat file: {e}")


async def _heartbeat_loop(stop_event: asyncio.Event):
    """🟢 Tarea asíncrona en segundo plano que escribe el heartbeat cada 30s
    sin depender de la ejecución del bucle principal de reintentos."""
    while not stop_event.is_set():
        _touch_heartbeat()
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

    # 🟢 FIX MANEJO SIGTERM/SIGINT: Captura señales de parada enviadas por AWS ECS Fargate
    def _stop_handler():
        logger.info("🛑 Recibida señal de apagado (SIGTERM/SIGINT). Iniciando cierre seguro...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop_handler)
        except (NotImplementedError, AttributeError):
            # Fallback para entornos donde add_signal_handler no esté soportado (ej. Windows)
            pass

    _touch_heartbeat()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event))
    logger.info("🟢 Worker activo y escuchando eventos/reintentos de la cola Redis...")

    try:
        # Bloquea hasta que se reciba SIGTERM/SIGINT
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