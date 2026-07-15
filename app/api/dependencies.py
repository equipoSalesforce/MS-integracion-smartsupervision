# app/api/dependencies.py
import boto3
import logging
from app.core.config import settings
from app.core.security.signatures import SfcSignatureContext
from app.core.auth import SfcAuthManager
from app.integrations.sfc_client import SfcClient

logger = logging.getLogger(__name__)

def get_s3_client():
    """
    Inicializa el cliente de AWS S3 de manera dinámica.
    - Local / CI: Usa AWS_ACCESS_KEY_ID y AWS_SECRET_ACCESS_KEY si existen.
    - Dev / Prod en Fargate: Usa el IAM Role asignado por AWS de forma transparente.
    - Local de Pruebas: Retorna None si no hay credenciales ni rol de AWS configurado.
    """
    try:
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            logger.info("Inicializando S3 Client usando credenciales explícitas.")
            return boto3.client(
                "s3",
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_REGION
            )
        
        # En AWS ECS Fargate, boto3 busca y hereda el IAM Role automáticamente
        logger.info("Buscando IAM Role en el ambiente para S3 Client.")
        return boto3.client("s3", region_name=settings.AWS_REGION)
    except Exception as e:
        logger.warning(
            f"No se pudo inicializar AWS S3. Los archivos no se procesarán de forma remota. "
            f"Esto es normal en entornos locales de desarrollo. Detalle: {str(e)}"
        )
        return None


def get_sfc_client() -> SfcClient:
    """
    Dependencia para obtener el cliente HTTP asíncrono configurado 
    con el interceptor de firmas HMAC de la SFC.
    """
    signature_context = SfcSignatureContext(settings.SFC_SECRET_KEY)
    auth_manager = SfcAuthManager(signature_context)
    
    return SfcClient(interceptor=auth_manager)