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
from app.core.middleware import CorrelationIdMiddleware, MaxBodySizeMiddleware
from app.core.mapping import SfcSalesforceMapper
from app.integrations.sfc_client import log_request, log_response
from app.services.crm_webhook_service import get_crm_webhook_client, close_crm_webhook_client
from app.api.dependencies import _auth_manager_instance
from app.core.security.signatures import ssl_context

from app.db.redis import init_redis, close_redis
from app.services.email_service import EmailAlertService
from app.workers.scheduler import iniciar_scheduler, detener_scheduler

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ciclo de vida de la aplicación.
    Inicializa y destruye ordenadamente los recursos globales del sistema.
    """
    logger.info(
        f"Arrancando {settings.PROJECT_NAME} en ambiente: {settings.ENVIRONMENT} "
        f"con Centralizada Redis + APScheduler activos."
    )

    timeout_sfc = httpx.Timeout(connect=3.0, read=15.0, write=10.0, pool=10.0)
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
    logger.info("📡 Pool global HTTP Client (SFC) inicializado con TLS 1.2 y Timeout extendido (read=15s).")

    get_crm_webhook_client()

    try:
        await init_redis()
        logger.info("Cliente de Redis centralizado inicializado correctamente.")
    except Exception as e:
        logger.error(f"Error crítico al inicializar Redis: {str(e)}")

    if settings.RUN_SCHEDULER:
        try:
            iniciar_scheduler()
            logger.info("🚀 Scheduler de reintentos para la SFC iniciado exitosamente en este nodo.")
        except Exception as e:
            logger.error(f"Fallo al arrancar el scheduler de reintentos: {str(e)}")
    else:
        logger.info("ℹ️ Scheduler desactivado para esta instancia Web (Modo Stateless API).")
    
    try:
        await SfcErrorTranslator.obtener_matriz_errores()
        await SfcSalesforceMapper.obtener_catalogos_y_mapeos()
    except Exception as e:
        # 🟢 FIX (revisión despliegue AWS): estos catálogos sólo se cargan aquí, en el
        # arranque -- no hay refresco perezoso por request, y /health/ready no los
        # valida (sólo revisa Redis). Antes, un fallo transitorio (p.ej. OAuth/Google
        # Sheets caído en el boot) dejaba CATALOGOS vacío y el contenedor igual pasaba
        # a servir tráfico del ALB, mapeando/validando cada despacho contra un
        # catálogo vacío de forma indefinida hasta un reinicio manual. Se prefiere
        # fallar rápido: si esto no carga, el contenedor no debe arrancar -- ECS
        # reintentará el despliegue y el circuit breaker/alarmas lo detectan.
        logger.critical(f"🔥 Fallo crítico al precargar catálogos/errores en RAM: {str(e)}")
        raise

    yield

    logger.info("🛑 Deteniendo servicios para apagado seguro...")
    
    try:
        await detener_scheduler()
    except Exception as e:
        logger.error(f"Error al detener scheduler: {e}")

    try:
        await close_redis()
    except Exception as e:
        logger.error(f"Error al cerrar Redis: {e}")

    try:
        await close_crm_webhook_client()
    except Exception as e:
        logger.error(f"Error al cerrar CRM Webhook Client: {e}")

    try:
        await _auth_manager_instance.close()
    except Exception as e:
        logger.error(f"Error al cerrar SfcAuthManager: {e}")

    if hasattr(app.state, "http_client"):
        try:
            await app.state.http_client.aclose()
            logger.info("📡 Pool global HTTP (SFC) liberado limpiamente.")
        except Exception as e:
            logger.error(f"Error al cerrar http_client global: {e}")

    logger.info(f"Apagando {settings.PROJECT_NAME} de manera limpia y segura.")
    
    await EmailAlertService.shutdown(timeout_segundos=3.0)

    try:
        await detener_scheduler()
    except Exception as e:
        logger.error(f"Error al detener scheduler: {e}")


# 🟢 FIX HALLAZGO 28: Deshabilitar Swagger UI (/docs), ReDoc (/redoc) y esquema OpenAPI (/openapi.json)
# fuera de entornos locales de desarrollo a menos que ENABLE_DOCS=True.
def _construir_app_fastapi(cfg=None) -> FastAPI:
    """
    Aísla el cómputo de docs_url/redoc_url/openapi_url en una función para que se pueda
    reconstruir con un `Settings` distinto en tests, sin depender del objeto `app` ya
    cacheado por `sys.modules` (docs_url se fija una sola vez al construir FastAPI(), así
    que parchear `settings` después de que otro módulo ya importó `app.main` no lo cambia).
    """
    cfg = cfg or settings
    es_entorno_local = cfg.ENVIRONMENT.strip().lower() in ("local", "development")
    permitir_docs = cfg.ENABLE_DOCS or es_entorno_local

    return FastAPI(
        title=cfg.PROJECT_NAME,
        openapi_url=f"{cfg.API_V1_STR}/openapi.json" if permitir_docs else None,
        docs_url="/docs" if permitir_docs else None,
        redoc_url="/redoc" if permitir_docs else None,
        lifespan=lifespan
    )


app = _construir_app_fastapi()

# 🌐 Registramos Middleware de Correlation ID y AWS Trace ID
app.add_middleware(CorrelationIdMiddleware)

origins = [str(origin) for origin in settings.CRM_CORS_ORIGINS]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key", "X-Correlation-ID"],
)

# 🟢 FIX HALLAZGO 41: se registra al final para que quede como capa MÁS externa
# (Starlette envuelve con el último middleware agregado por fuera de los anteriores),
# rechazando requests demasiado grandes antes de que CORS/Correlation-ID hagan trabajo.
app.add_middleware(MaxBodySizeMiddleware, max_body_size=settings.MAX_REQUEST_BODY_SIZE_BYTES)

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