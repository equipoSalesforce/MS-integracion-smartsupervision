# app/worker.py
import asyncio
import logging
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db.redis import init_redis, close_redis
from app.workers.scheduler import iniciar_scheduler, detener_scheduler
from app.core.exceptions import SfcErrorTranslator
from app.core.mapping import SfcSalesforceMapper
from app.services.crm_webhook_service import close_crm_webhook_client
from app.api.dependencies import _auth_manager_instance  # 🟢 Importar singleton

setup_logging()
logger = logging.getLogger("worker_process")

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
    
    logger.info("🟢 Worker activo y escuchando eventos/reintentos de la cola Redis...")

    # Loop asíncrono infalible para mantener el proceso vivo en Docker/ECS
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Recibida señal de apagado. Apagando Worker de forma segura...")
    finally:
        detener_scheduler()
        await close_redis()
        await close_crm_webhook_client()
        await _auth_manager_instance.close()  # 🟢 Cierre explícito de la sesión de autenticación
        logger.info("👋 Worker detenido completamente.")

if __name__ == "__main__":
    asyncio.run(run_worker_process())