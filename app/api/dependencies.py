# app/api/dependencies.py
import boto3
import logging
from typing import Optional
from fastapi import Security, HTTPException, status, Request
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
# Se instancian una sola vez cuando la aplicación arranca.
_signature_context_instance = SfcSignatureContext(settings.SFC_SECRET_KEY)
_auth_manager_instance = SfcAuthManager(_signature_context_instance)


def get_s3_client():
    """Inicializa el cliente de AWS S3 dinámicamente."""
    try:
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            logger.info("Inicializando S3 Client usando credenciales explícitas.")
            return boto3.client(
                "s3",
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_REGION,
                endpoint_url=getattr(settings, "AWS_S3_ENDPOINT_URL", None) 
            )
        
        logger.info("Buscando IAM Role en el ambiente para S3 Client.")
        return boto3.client("s3", region_name=settings.AWS_REGION)
    except Exception as e:
        logger.warning(f"No se pudo inicializar AWS S3: {str(e)}")
        return None


def get_sfc_client(request: Request = None) -> SfcClient:
    """
    Inyecta SfcClient reutilizando la misma instancia de _auth_manager_instance.
    Esto permite conservar el access_token y refresh_token en memoria RAM.
    """
    http_client = None
    if request and hasattr(request, "app") and hasattr(request.app, "state") and hasattr(request.app.state, "http_client"):
        http_client = request.app.state.http_client

    return SfcClient(interceptor=_auth_manager_instance, http_client=http_client)


async def verificar_api_key_crm(api_key: str = Security(api_key_header)) -> str:
    """Valida la API Key enviada en la cabecera 'X-API-Key'."""
    if api_key != settings.CRM_API_KEY:
        logger.warning("Intento de acceso no autorizado con API Key inválida.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Acceso denegado: API Key inválida o no proporcionada.",
        )
    return api_key