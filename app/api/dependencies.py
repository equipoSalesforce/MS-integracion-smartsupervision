# app/api/dependencies.py
import os
import secrets
import time
import logging
import boto3
import httpx
from fastapi import Depends, Header, HTTPException, status, Request
from fastapi.security.api_key import APIKeyHeader

from app.core.config import settings
from app.core.security.signatures import SfcSignatureContext
from app.core.auth import SfcAuthManager
from app.integrations.sfc_client import SfcClient
from app.db.redis import get_redis_client

logger = logging.getLogger(__name__)

RATE_LIMIT_PREFIX = "{sfc:ratelimit}"

# 🛡️ Esquema de seguridad
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=True)

# 🎯 SINGLETONS A NIVEL DE MÓDULO
_signature_context_instance = SfcSignatureContext(settings.SFC_SECRET_KEY)
_auth_manager_instance = SfcAuthManager(_signature_context_instance)
_s3_client_instance = None  # 🟢 Instancia Singleton retenida en memoria RAM


async def verificar_api_key_admin(
    x_api_key: str = Header(..., alias="X-API-Key")
) -> str:
    """
    Verifica que la cabecera X-API-Key coincida con la API Key administrativa (ADMIN_API_KEY).
    Protege endpoints administrativos que muestran telemetría o estado de colas.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cabecera X-API-Key faltante."
        )

    # 🔒 Comparación segura en tiempo constante para evitar Timing Attacks
    # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): secrets.compare_digest lanza
    # TypeError si cualquiera de los dos strings tiene caracteres no-ASCII -- sin este
    # chequeo, un header con esos caracteres propagaba la excepción sin capturar,
    # devolviendo 500 (y un log logger.critical de "error no controlado") en vez del
    # 401 correcto para una credencial inválida.
    es_valida = x_api_key.isascii() and secrets.compare_digest(x_api_key, settings.ADMIN_API_KEY)

    if not es_valida:
        logger.warning("🔐 [Seguridad] Intento de acceso administrativo no autorizado con X-API-Key inválida.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key administrativa inválida o no autorizada."
        )

    return x_api_key


def get_s3_client():
    """
    Obtiene o inicializa de forma perezosa la instancia Singleton del cliente Boto3 S3.
    Reutiliza la sesión y pool de conexiones, eliminando la latencia de re-autenticación por request.
    """
    global _s3_client_instance
    if _s3_client_instance is None:
        try:
            
            endpoint_url = (
                settings.AWS_S3_ENDPOINT_URL or 
                getattr(settings, "AWS_S3_ENDPOINT_URL", None) or 
                os.getenv("AWS_S3_ENDPOINT_URL")
            )
            
            if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                logger.info("Inicializando S3 Client Singleton usando credenciales explícitas/STS.")
                kwargs = {
                    "aws_access_key_id": settings.AWS_ACCESS_KEY_ID,
                    "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY,
                    "region_name": settings.AWS_REGION,
                }
                
                # 🟢 FIX: Si existen credenciales temporales (ASIA...), inyecta el token de sesión
                session_token = settings.AWS_SESSION_TOKEN
                if session_token:
                    kwargs["aws_session_token"] = session_token

                if endpoint_url:
                    kwargs["endpoint_url"] = endpoint_url

                _s3_client_instance = boto3.client("s3", **kwargs)
            else:
                logger.info("Buscando IAM Role en el ambiente para S3 Client Singleton.")
                kwargs = {"region_name": settings.AWS_REGION}
                if endpoint_url:
                    kwargs["endpoint_url"] = endpoint_url

                _s3_client_instance = boto3.client("s3", **kwargs)
        except Exception as e:
            logger.warning(f"No se pudo inicializar AWS S3 Singleton: {str(e)}")
            _s3_client_instance = None

    return _s3_client_instance


def get_sfc_client(request: Request = None) -> SfcClient:
    """
    Inyecta SfcClient reutilizando la misma instancia de _auth_manager_instance.
    Esto permite conservar el access_token y refresh_token en memoria RAM.

    Usado como dependencia de FastAPI (`Depends(get_sfc_client)`) -- su firma debe
    seguir siendo introspectable por FastAPI, por eso NO recibe un `http_client`
    explícito aquí. El worker/scheduler (que no tiene `Request` ni ciclo de vida
    FastAPI) usa `get_sfc_client_con_http_client()` en su lugar.
    """
    http_client = None
    if request and hasattr(request, "app") and hasattr(request.app, "state") and hasattr(request.app.state, "http_client"):
        http_client = request.app.state.http_client

    return SfcClient(interceptor=_auth_manager_instance, http_client=http_client)


def get_sfc_client_con_http_client(http_client: httpx.AsyncClient) -> SfcClient:
    """
    Variante de get_sfc_client() para contextos sin `Request`/FastAPI (el proceso
    worker). Recibe el httpx.AsyncClient explícitamente para que SfcClient lo
    reutilice en vez de crear (y filtrar) uno propio en cada llamada.
    """
    return SfcClient(interceptor=_auth_manager_instance, http_client=http_client)


async def verificar_api_key_crm(
    x_api_key: str = Header(..., alias="X-API-Key")
) -> str:
    """
    Verifica que la cabecera X-API-Key coincida con la configurada para el CRM.
    Utiliza tiempo constante (compare_digest) para evitar ataques de tiempo (Timing Attacks).
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cabecera X-API-Key faltante."
        )

    # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): ver el mismo fix en
    # verificar_api_key_admin -- compare_digest lanza TypeError con no-ASCII.
    es_valida = x_api_key.isascii() and secrets.compare_digest(x_api_key, settings.CRM_API_KEY)

    if not es_valida:
        logger.warning("🔐 [Seguridad] Intento de acceso no autorizado con X-API-Key inválida.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key inválida o no autorizada."
        )

    return x_api_key


async def verificar_rate_limit_crm(x_api_key: str = Depends(verificar_api_key_crm)) -> None:
    """
    🟢 Rate limit por API key para los endpoints que consume el CRM (revisión de
    seguridad, 2026-08-27): protege contra un bucle/bug del lado del CRM que sature
    la cuota de la SFC o el pool de conexiones -- no un límite pensado para tráfico
    legítimo bajo condiciones normales.

    Ventana fija en Redis vía INCR + EXPIRE: sólo el primer INCR de cada ventana
    (resultado == 1) fija el TTL, así que no hay ventana de carrera en la que dos
    requests concurrentes pisen el EXPIRE del otro -- INCR es atómico, únicamente
    una de ellas puede ver el resultado 1.

    Encadena `Depends(verificar_api_key_crm)` -- corre DESPUÉS de validar la key
    (nunca cuenta ni bloquea intentos con key inválida, eso es un problema distinto)
    y reutiliza el mismo valor ya validado (FastAPI cachea la dependencia por
    request, no se re-ejecuta el chequeo de key dos veces).

    Fail-open ante cualquier fallo de Redis: a diferencia de IdempotencyService (que
    debe fallar cerrado para no arriesgar un duplicado ante la SFC), el rate limit es
    una capa de defensa adicional -- una caída de Redis no debe sumar un modo de
    fallo nuevo sobre el que IdempotencyService ya maneja fail-closed por su cuenta.
    """
    if not settings.CRM_RATE_LIMIT_ENABLED:
        return

    redis = get_redis_client()
    if not redis:
        return

    ventana_actual = int(time.time() // settings.CRM_RATE_LIMIT_WINDOW_SECONDS)
    clave = f"{RATE_LIMIT_PREFIX}:{x_api_key}:{ventana_actual}"

    try:
        conteo = await redis.incr(clave)
        if conteo == 1:
            await redis.expire(clave, settings.CRM_RATE_LIMIT_WINDOW_SECONDS)
    except Exception as e:
        logger.warning(f"⚠️ [Rate Limit CRM] No se pudo consultar/actualizar el contador en Redis: {e}")
        return

    if conteo > settings.CRM_RATE_LIMIT_MAX_REQUESTS:
        logger.warning(
            f"🚦 [Rate Limit CRM] Límite excedido: {conteo} solicitudes en la ventana actual "
            f"({settings.CRM_RATE_LIMIT_MAX_REQUESTS} permitidas cada {settings.CRM_RATE_LIMIT_WINDOW_SECONDS}s)."
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "status_code": 429,
                "error_type": "RATE_LIMIT_EXCEEDED",
                "sfc_field": None,
                "raw_message": (
                    f"Se superó el límite de {settings.CRM_RATE_LIMIT_MAX_REQUESTS} solicitudes "
                    f"por {settings.CRM_RATE_LIMIT_WINDOW_SECONDS} segundos."
                ),
                "crm_action_friendly": "Reduzca la frecuencia de solicitudes y reintente en unos segundos."
            },
            headers={"Retry-After": str(settings.CRM_RATE_LIMIT_WINDOW_SECONDS)}
        )