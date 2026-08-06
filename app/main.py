# app/main.py
import logging
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api import routes_health
from app.core.config import settings
from app.api.routes_quejas import router as quejas_router
from app.core.exceptions import SfcErrorTranslator, SfcIntegrationException
from app.core.logging_config import setup_logging
from app.core.middleware import CorrelationIdMiddleware
from app.core.mapping import SfcSalesforceMapper
from app.integrations.sfc_client import ssl_context, log_request, log_response
from app.services.crm_webhook_service import get_crm_webhook_client, close_crm_webhook_client

from app.db.redis import init_redis, close_redis
from app.workers.scheduler import iniciar_scheduler, detener_scheduler

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ciclo de vida de la aplicación.
    Inicializa y destruye ordenadamente los recursos globales del sistema:
    - Pool HTTP TLS 1.2 (SFC)
    - Pool HTTP CRM Webhook
    - Conexiones a Redis
    - Tareas en segundo plano (APScheduler)
    """
    logger.info(
        f"Arrancando {settings.PROJECT_NAME} en ambiente: {settings.ENVIRONMENT} "
        f"con Centralizada Redis + APScheduler activos."
    )

    # 1. Pool Global HTTP para la SFC (TLS 1.2 + Hooks de Auditoría)
    timeout_sfc = httpx.Timeout(connect=2.0, read=3.0, write=5.0, pool=5.0)
    limits_sfc = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    
    app.state.http_client = httpx.AsyncClient(
        timeout=timeout_sfc, 
        limits=limits_sfc,
        verify=ssl_context,
        event_hooks={
            'request': [log_request],
            'response': [log_response]
        }
    )
    logger.info("📡 Pool global HTTP Client (SFC) inicializado con TLS 1.2.")

    # 2. Pool HTTP para el CRM Webhook
    get_crm_webhook_client()

    # 3. Inicializar cliente Redis centralizado
    try:
        await init_redis()
        logger.info("Cliente de Redis centralizado inicializado correctamente.")
    except Exception as e:
        logger.error(f"Error crítico al inicializar Redis: {str(e)}")

    # 4. Encender el scheduler de reintentos
    try:
        iniciar_scheduler()
        logger.info("Scheduler de reintentos para la SFC iniciado exitosamente.")
    except Exception as e:
        logger.error(f"Fallo al arrancar el scheduler de reintentos: {str(e)}")
    
    # 5. Cargar matriz de errores y catálogos en RAM
    try:
        await SfcErrorTranslator.obtener_matriz_errores()
        await SfcSalesforceMapper.obtener_catalogos_y_mapeos()
    except Exception as e:
        logger.error(f"Fallo al precargar catálogos/errores en RAM: {str(e)}")

    yield

    # ======================================================================
    # 🛑 CIERRE LIMPIO DE RECURSOS (SHUTDOWN)
    # ======================================================================
    logger.info("🛑 Deteniendo servicios para apagado seguro...")
    
    detener_scheduler()
    await close_redis()
    await close_crm_webhook_client()

    if hasattr(app.state, "http_client"):
        await app.state.http_client.aclose()
        logger.info("📡 Pool global HTTP (SFC) liberado limpiamente.")

    logger.info(f"Apagando {settings.PROJECT_NAME} de manera limpia y segura.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# 🌐 Registramos Middleware de Correlation ID
app.add_middleware(CorrelationIdMiddleware)

# 🌐 Configuración de CORS
origins = [str(origin) for origin in settings.BACKEND_CORS_ORIGINS] if settings.BACKEND_CORS_ORIGINS else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key", "X-Correlation-ID"],
)

# ======================================================================
# 🛡️ EXCEPTION HANDLERS
# ======================================================================

@app.exception_handler(RequestValidationError)
@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(
    request: Request, 
    exc: RequestValidationError | ValidationError
):
    errors = exc.errors()
    if errors:
        first_error = errors[0]
        loc = first_error.get("loc", [])
        field_name = str(loc[-1]) if loc and loc[-1] != "body" else "payload"
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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    correlation_id = getattr(request.state, "correlation_id", "N/A")
    logger.critical(f"🔥 Error no controlado en endpoint '{request.url.path}' [CID: {correlation_id}]: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status_code": 500,
            "error_type": "INTERNAL_SERVER_ERROR",
            "sfc_field": None,
            "raw_message": "Ocurrió un error interno no esperado en el microservicio.",
            "crm_action_friendly": "Reintente la operación más tarde o contacte al administrador de integración."
        }
    )


@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
async def health_check():
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "project": settings.PROJECT_NAME
    }

app.include_router(
    quejas_router,
    prefix=f"{settings.API_V1_STR}/quejas",
    tags=["Quejas"]
)

app.include_router(routes_health.router)