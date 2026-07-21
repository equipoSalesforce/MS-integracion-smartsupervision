# app/main.py
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.api.routes_quejas import router as quejas_router
from app.core.logging_config import setup_logging

setup_logging()

# Inicialización del Logger de la aplicación
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ciclo de vida de la aplicación (Stateless Gateway).
    Gestiona de manera limpia el arranque y el apagado del contenedor 
    en entornos de nube como AWS ECS + Fargate.
    """
    # --- Lógica de Startup (Arranque) ---
    logger.info(
        f"Arrancando {settings.PROJECT_NAME} en ambiente: {settings.ENVIRONMENT} "
        f"como Gateway de Integración 100% Stateless."
    )

    yield

    # --- Lógica de Shutdown (Apagado) ---
    logger.info(f"Apagando {settings.PROJECT_NAME} limpiamente...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# 🌐 Configuración de CORS (Cross-Origin Resource Sharing)
# Permite peticiones del CRM local o web y habilita la cabecera 'X-API-Key'
origins = [str(origin) for origin in settings.BACKEND_CORS_ORIGINS] if settings.BACKEND_CORS_ORIGINS else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key"],  # 👈 Aseguramos que permita el header de autenticación
)

# --- Endpoint Crítico de AWS ALB Health Check (Público / Sin API Key) ---
@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
async def health_check():
    """
    Health check síncrono para el Balanceador de Carga de AWS (ALB).
    Al no depender de base de datos local, no requiere validar conexiones 
    pesadas, resultando en respuestas inmediatas de alta confiabilidad.
    """
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "project": settings.PROJECT_NAME
    }

# --- REGISTRO DE RUTAS ---
# El router de quejas ya viene protegido por dentro con 'verificar_api_key_crm'
app.include_router(
    quejas_router,
    prefix=f"{settings.API_V1_STR}/quejas",
    tags=["Quejas"]
)