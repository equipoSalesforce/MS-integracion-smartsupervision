import boto3
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from typing import Generator

from app.core.config import settings
from app.core.security.signatures import SfcSignatureContext
from app.core.auth import SfcAuthManager
from app.integrations.sfc_client import SfcClient

logger = logging.getLogger(__name__)

# Base declarativa compartida por todos los modelos de SQLAlchemy
Base = declarative_base()

# Configuración del motor de Base de Datos para MySQL (AWS Aurora / RDS)
# pool_recycle ayuda a prevenir el error "MySQL server has gone away" al reciclar conexiones inactivas
engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db() -> Generator[Session, None, None]:
    """
    Dependencia de FastAPI para inyectar y cerrar automáticamente 
    la sesión de la base de datos en los endpoints.
    """
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


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
            f"No se pudo inicializar AWS S3. Los archivos no se guardarán en la nube. "
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