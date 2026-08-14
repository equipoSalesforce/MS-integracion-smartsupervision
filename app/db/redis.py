# app/db/redis.py
import asyncio
import logging
from typing import Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

# Instancia global del cliente Redis
redis_client = None
_reconnect_task: Optional[asyncio.Task] = None


def _build_redis_client():
    """
    Construye el cliente Redis/Valkey configurado con estrategias
    de resiliencia, timeouts de socket, health check y retry/backoff exponencial.
    """
    import redis.asyncio as aioredis
    from redis.asyncio.retry import Retry
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError, TimeoutError, BusyLoadingError

    # 🟢 Estrategia de reintentos automáticos ante cortes transitorios
    retry_strategy = Retry(
        ExponentialBackoff(cap=10, base=1),
        retries=5
    )

    common_kwargs = {
        "decode_responses": True,
        "health_check_interval": 15,          # Verifica la salud de conexiones inactivas en el pool cada 15s
        "socket_timeout": 5.0,                # Timeout para operaciones de lectura/escritura en socket
        "socket_connect_timeout": 5.0,        # Timeout para abrir nuevas conexiones TCP
        "socket_keepalive": True,             # Sondas TCP Keep-Alive a nivel de sistema operativo
        "retry_on_timeout": True,             # Reintenta automáticamente si ocurre un socket timeout
        "retry": retry_strategy,
        "retry_on_error": [ConnectionError, TimeoutError, BusyLoadingError]
    }

    use_cluster = settings.REDIS_CLUSTER_MODE

    if use_cluster:
        if settings.REDIS_URL:
            return aioredis.RedisCluster.from_url(settings.REDIS_URL, **common_kwargs)
        return aioredis.RedisCluster(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD or None,
            ssl=settings.REDIS_SSL,
            **common_kwargs
        )

    if settings.REDIS_URL:
        return aioredis.from_url(settings.REDIS_URL, **common_kwargs)

    return aioredis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None,
        db=settings.REDIS_DB,
        ssl=settings.REDIS_SSL,
        **common_kwargs
    )


async def init_redis() -> bool:
    """
    Inicializa la conexión global asíncrona a Redis durante el arranque.
    Si falla, programa un bucle de reconexión en segundo plano sin bloquear el inicio del servicio.
    """
    global redis_client
    try:
        if redis_client is None:
            redis_client = _build_redis_client()

        await redis_client.ping()
        logger.info("🟢 Conexión con servidor Redis establecida exitosamente.")
        return True
    except Exception as e:
        logger.warning(
            f"⚠️ [Redis Conexión] No se pudo establecer conexión inicial con Redis ({str(e)}). "
            f"El servicio operará de forma limitada e intentará reconectarse en segundo plano."
        )
        redis_client = None
        _iniciar_tarea_reconexion()
        return False


def _iniciar_tarea_reconexion():
    """Inicia la tarea asíncrona en segundo plano para intentar reconectarse a Redis."""
    global _reconnect_task
    if _reconnect_task is None or _reconnect_task.done():
        try:
            loop = asyncio.get_running_loop()
            _reconnect_task = loop.create_task(_reintentar_conexion_background())
        except RuntimeError:
            pass


async def _reintentar_conexion_background():
    """Loop en segundo plano que reintenta establecer la conexión inicial con Redis cada 10 segundos."""
    global redis_client
    logger.info("🔄 [Redis Auto-Reconnect] Tarea de reconexión automática en segundo plano iniciada...")
    
    while redis_client is None:
        await asyncio.sleep(10)
        candidate_client = None
        try:
            candidate_client = _build_redis_client()
            await candidate_client.ping()
            redis_client = candidate_client
            logger.info("🟢 [Redis Auto-Reconnect] Conexión con Redis restablecida exitosamente.")
            break
        except Exception as e:
            logger.debug(f"⏳ [Redis Auto-Reconnect] Reintento de conexión fallido: {e}")
            # 🟢 FIX P1-01: cerrar el cliente candidato fallido — si no, cada intento
            # fallido durante una caída prolongada deja un pool de conexiones abierto sin
            # referenciar, acumulándose indefinidamente.
            if candidate_client is not None:
                try:
                    await candidate_client.aclose()
                except Exception:
                    pass


async def close_redis():
    """
    Cierra limpiamente la tarea de reconexión y el pool de conexiones al apagar la aplicación.
    """
    global redis_client, _reconnect_task

    if _reconnect_task and not _reconnect_task.done():
        _reconnect_task.cancel()
        try:
            await _reconnect_task
        except asyncio.CancelledError:
            pass
        _reconnect_task = None

    if redis_client:
        try:
            await redis_client.aclose()
            logger.info("🛑 Pool de conexiones a Redis cerrado limpiamente.")
        except Exception as e:
            logger.error(f"Error al cerrar conexión con Redis: {str(e)}")
        finally:
            redis_client = None


def get_redis_client():
    """
    Inyecta la instancia activa del cliente de Redis.
    Si redis_client está en None, verifica e inicia la tarea de reconexión en segundo plano.
    """
    if redis_client is None:
        _iniciar_tarea_reconexion()
    return redis_client