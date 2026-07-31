# app/db/redis.py
import logging
from typing import Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

# Instancia global del cliente Redis
redis_client = None


async def init_redis():
    """
    Inicializa la conexión global asíncrona a Redis durante el arranque de la aplicación.
    """
    global redis_client
    try:
        import redis.asyncio as aioredis

        if settings.REDIS_URL:
            logger.info("📡 Conectando a Redis usando REDIS_URL...")
            redis_client = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True
            )
        else:
            logger.info(
                f"📡 Conectando a Redis en {settings.REDIS_HOST}:{settings.REDIS_PORT} "
                f"(DB: {settings.REDIS_DB}, SSL: {settings.REDIS_SSL})..."
            )
            redis_client = aioredis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                password=settings.REDIS_PASSWORD or None,
                db=settings.REDIS_DB,
                ssl=settings.REDIS_SSL,
                decode_responses=True
            )

        await redis_client.ping()
        logger.info("🟢 Conexión con servidor Redis establecida exitosamente.")
    except Exception as e:
        logger.warning(
            f"⚠️ [Redis Conexión] No se pudo establecer conexión con Redis ({str(e)}). "
            f"El servicio operará de forma limitada hasta que Redis esté disponible."
        )
        redis_client = None


async def close_redis():
    """
    Cierra limpiamente el pool de conexiones de Redis al apagar la aplicación.
    """
    global redis_client
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
    """
    return redis_client