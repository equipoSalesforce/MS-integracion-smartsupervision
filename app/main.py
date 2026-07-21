import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.api.routes_quejas import router as quejas_router
from app.core.logging_config import setup_logging

# 🛠️ Nuevos imports para SQLite y el Scheduler
from app.db.database import init_db
from app.workers.scheduler import iniciar_scheduler, detener_scheduler

setup_logging()

# Inicialización del Logger de la aplicación
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ciclo de vida de la aplicación.
    Inicializa la base de datos SQLite local y el motor de reintentos
    en segundo plano (APScheduler) antes de recibir tráfico.
    """
    # --- Lógica de Startup (Arranque) ---
    logger.info(
        f"Arrancando {settings.PROJECT_NAME} en ambiente: {settings.ENVIRONMENT} "
        f"con Cola Local SQLite + APScheduler activos."
    )

    # 1. Crear la tabla SQLite de la cola si no existe
    try:
        await init_db()
        logger.info("Base de datos SQLite local inicializada correctamente.")
    except Exception as e:
        logger.error(f"Error crítico al inicializar SQLite local: {str(e)}")

    # 2. Encender el scheduler de reintentos
    try:
        iniciar_scheduler()
        logger.info("Scheduler de reintentos para la SFC iniciado exitosamente.")
    except Exception as e:
        logger.error(f"Fallo al arrancar el scheduler de reintentos: {str(e)}")

    yield

    # --- Lógica de Shutdown (Apagado) ---
    logger.info("Deteniendo scheduler de reintentos...")
    detener_scheduler()
    logger.info(f"Apagando {settings.PROJECT_NAME} limpiamente...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# 🌐 Configuración de CORS (Cross-Origin Resource Sharing)
origins = [str(origin) for origin in settings.BACKEND_CORS_ORIGINS] if settings.BACKEND_CORS_ORIGINS else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key"],
)

# --- Endpoint Crítico de AWS ALB Health Check ---
@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
async def health_check():
    """
    Health check síncrono para el Balanceador de Carga (ALB).
    """
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "project": settings.PROJECT_NAME
    }

# --- REGISTRO DE RUTAS ---
app.include_router(
    quejas_router,
    prefix=f"{settings.API_V1_STR}/quejas",
    tags=["Quejas"]
)