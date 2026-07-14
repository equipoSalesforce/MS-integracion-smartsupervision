from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.core.config import settings
from app.core.auth import SfcAuthManager
from app.core.security.signatures import SfcSignatureContext
from app.integrations.sfc_interceptor import SfcRequestInterceptor
from app.integrations.sfc_client import SfcClient

# 1. Configuración de Base de Datos Dinámica
engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db() -> Generator[Session, None, None]:
    """Generador de sesiones de base de datos por petición."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 2. Configuración de Clientes SFC
signature_context = SfcSignatureContext(secret_key=settings.SFC_SECRET_KEY)
auth_manager = SfcAuthManager(signature_context=signature_context)
interceptor = SfcRequestInterceptor(secret_key=settings.SFC_SECRET_KEY, auth_manager=auth_manager)

def get_sfc_client() -> SfcClient:
    return SfcClient(interceptor=interceptor)