import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.routes_quejas import router as quejas_router

# Inicialización del Logger de la aplicación
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ciclo de vida del microservicio.
    Aquí se gestiona la inicialización de conexiones pesadas al arrancar 
    y el cierre limpio de recursos al apagar el contenedor.
    """
    # --- Lógica de Startup (Arranque) ---
    logger.info(f"Arrancando {settings.PROJECT_NAME} en ambiente: {settings.ENVIRONMENT}")

    yield

    # --- Lógica de Shutdown (Apagado) ---
    logger.info(f"Apagando {settings.PROJECT_NAME} limpiamente...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# Configuración de CORS (Cross-Origin Resource Sharing)
if settings.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# --- Endpoint Crítico para AWS ECS + Fargate ---
@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
async def health_check():
    """
    Endpoint utilizado por el Target Group del Balanceador de Carga de AWS (ALB).
    Si este endpoint no retorna HTTP 200, AWS asumirá que el contenedor falló
    y lo reemplazará automáticamente sin interrumpir el servicio.
    """
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "project": settings.PROJECT_NAME
    }

# --- REGISTRO DE RUTAS ---
# Monta las rutas de app/api/routes_quejas.py bajo el prefijo "/api/v1/quejas"
# y las agrupa ordenadamente bajo la sección "Quejas" en la interfaz de Swagger.
app.include_router(
    quejas_router,
    prefix=f"{settings.API_V1_STR}/quejas",
    tags=["Quejas"]
)