# app/main.py
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.core.config import settings
from app.api.routes_quejas import router as quejas_router
from app.core.exceptions import SfcErrorTranslator, SfcIntegrationException
from app.core.logging_config import setup_logging

# 🛠️ Imports para SQLite y Scheduler
from app.db.database import init_db
from app.workers.scheduler import iniciar_scheduler, detener_scheduler

setup_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ciclo de vida de la aplicación.
    Inicializa la base de datos SQLite local y el motor de reintentos
    en segundo plano (APScheduler) antes de recibir tráfico.
    """
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
    
    # 3. Cargar matriz de errores en RAM
    try:
        await SfcErrorTranslator.obtener_matriz_errores()
    except Exception as e:
        logger.error(f"Fallo al arrancar la matriz de errores: {str(e)}")
    
    yield

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

# ======================================================================
# 🛡️ EXCEPTION HANDLERS: Formato Canónico de Errores para el CRM
# ======================================================================

@app.exception_handler(RequestValidationError)
@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(
    request: Request, 
    exc: RequestValidationError | ValidationError
):
    """
    Intercepta errores de validación de Pydantic/FastAPI y los formatea 
    al estándar de respuesta esperado por Salesforce/CRM.
    """
    errors = exc.errors()
    if errors:
        first_error = errors[0]
        loc = first_error.get("loc", [])
        
        # Obtenemos el nombre exacto del campo con error (ej: 'Categorias_COL__c')
        field_name = str(loc[-1]) if loc and loc[-1] != "body" else "payload"
        
        # Limpiamos el mensaje de Pydantic
        raw_msg = first_error.get("msg", "Error de validación en el payload.")
        if raw_msg.startswith("Value error, "):
            raw_msg = raw_msg.replace("Value error, ", "", 1)
    else:
        field_name = "payload"
        raw_msg = "Estructura del payload JSON inválida."

    crm_action = (
        f"Verifique el campo '{field_name}' en el CRM/Salesforce. "
        f"Asegúrese de cumplir con el tipo de dato, restricciones y catálogo permitido."
    )

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "status_code": 400,
            "error_type": "CRM_PAYLOAD_VALIDATION_ERROR",
            "sfc_field": field_name,
            "raw_message": raw_msg,
            "crm_action_friendly": crm_action
        }
    )


@app.exception_handler(SfcIntegrationException)
async def sfc_integration_exception_handler(request: Request, exc: SfcIntegrationException):
    """
    Retorna las excepciones traducidas de la SFC manteniendo el formato estructurado.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status_code": exc.status_code,
            "error_type": exc.error_type,
            "sfc_field": exc.sfc_field,
            "raw_message": exc.raw_message,
            "crm_action_friendly": exc.crm_action
        }
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