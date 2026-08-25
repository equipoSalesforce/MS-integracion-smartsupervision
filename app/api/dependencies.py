# app/api/dependencies.py
import os
import secrets
import logging
import boto3
import httpx
from fastapi import Header, HTTPException, status, Request
from fastapi.security.api_key import APIKeyHeader

from app.core.config import settings
from app.core.security.signatures import SfcSignatureContext
from app.core.auth import SfcAuthManager
from app.integrations.sfc_client import SfcClient

logger = logging.getLogger(__name__)

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
    es_valida = secrets.compare_digest(x_api_key, settings.ADMIN_API_KEY)

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

    es_valida = secrets.compare_digest(x_api_key, settings.CRM_API_KEY)

    if not es_valida:
        logger.warning("🔐 [Seguridad] Intento de acceso no autorizado con X-API-Key inválida.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key inválida o no autorizada."
        )

    return x_api_key