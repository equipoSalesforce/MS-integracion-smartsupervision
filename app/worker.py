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
from app.services.email_service import EmailAlertService
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


# 🟢 FIX P1-14: nº de fallos consecutivos de Redis tras los cuales se alerta por
# correo (además de loguear). A 30s por ciclo, 10 fallos ~= 5 minutos degradado.
REDIS_FALLOS_CONSECUTIVOS_PARA_ALERTAR = 10


async def _heartbeat_loop(stop_event: asyncio.Event):
    """
    🟢 FIX HALLAZGO 38 / P1-14: el heartbeat de liveness del proceso (que Docker/ECS
    usa para decidir si reiniciar el contenedor) y la salud de Redis son señales
    DISTINTAS. Antes, este loop omitía tocar el archivo de heartbeat si Redis estaba
    caído — eso hacía que una caída/mantenimiento de Redis (que afecta a TODAS las
    réplicas del worker por igual) marcara TODOS los contenedores como unhealthy al
    mismo tiempo, y ECS los reiniciaba en masa sin que eso resolviera nada (Redis
    seguía caído para los contenedores nuevos también). Ahora el heartbeat siempre
    se actualiza mientras el event loop esté vivo y respondiendo (la verdadera
    señal de liveness); la salud de Redis se rastrea aparte y sólo genera una
    alerta operativa tras varios fallos consecutivos sostenidos, sin disparar
    reinicios de contenedor.
    """
    fallos_consecutivos_redis = 0
    degradado_notificado = False

    while not stop_event.is_set():
        is_redis_ok = await _check_redis_health()
        _touch_heartbeat()

        if is_redis_ok:
            if degradado_notificado:
                logger.info("💚 [Worker Heartbeat] Redis se recuperó tras estar degradado.")
            fallos_consecutivos_redis = 0
            degradado_notificado = False
            logger.debug("💚 [Worker Heartbeat] Heartbeat actualizado (Redis OK).")
        else:
            fallos_consecutivos_redis += 1
            logger.warning(
                f"⚠️ [Worker Heartbeat] Redis inalcanzable o degradado (fallo consecutivo "
                f"#{fallos_consecutivos_redis}). El heartbeat de liveness se mantiene activo "
                f"para no forzar reinicios de contenedor por una dependencia externa caída."
            )
            if fallos_consecutivos_redis >= REDIS_FALLOS_CONSECUTIVOS_PARA_ALERTAR and not degradado_notificado:
                degradado_notificado = True
                await EmailAlertService.notificar_falla_infraestructura(
                    smart_code="WORKER_REDIS_HEALTHCHECK",
                    error_msg=(
                        f"El worker lleva {fallos_consecutivos_redis} verificaciones consecutivas "
                        f"({fallos_consecutivos_redis * HEARTBEAT_INTERVAL_SECONDS}s aprox.) sin poder "
                        f"conectarse a Redis. El proceso sigue vivo; requiere revisión de la "
                        f"disponibilidad de Redis/ElastiCache."
                    )
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

    # 🟢 FIX P1-14: se toca el heartbeat de arranque incondicionalmente — el proceso
    # ya está vivo y con su event loop corriendo en este punto, independientemente de
    # si Redis responde. Antes, si Redis estaba caído justo al arrancar (p.ej. un
    # despliegue durante un mantenimiento/failover de ElastiCache), el archivo de
    # heartbeat nunca se creaba y el contenedor podía marcarse unhealthy antes de que
    # Redis tuviera oportunidad de recuperarse.
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

        await detener_scheduler()
        # 🟢 FIX P1-07: el worker no esperaba las alertas de correo en vuelo antes de
        # cerrar — a diferencia de app/main.py, que sí lo hace en su lifespan. Un
        # SIGTERM de ECS (deploy/scale-in) podía perder alertas ya programadas.
        await EmailAlertService.shutdown(timeout_segundos=3.0)
        await close_redis()
        await close_crm_webhook_client()
        await _auth_manager_instance.close()
        logger.info("👋 Worker detenido completamente de forma segura.")


if __name__ == "__main__":
    asyncio.run(run_worker_process())