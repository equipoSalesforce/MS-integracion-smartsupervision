# app/core/config.py
from typing import List, Any
from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Annotated

def parse_cors(v: Any) -> List[str]:
    if isinstance(v, str) and not v.startswith("["):
        return [i.strip() for i in v.split(",")]
    elif isinstance(v, (list, str)):
        return v
    raise ValueError(v)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore"
    )

    # --- Configuración Base ---
    PROJECT_NAME: str = "MS-integracion-smartsupervision"
    ENVIRONMENT: str = "local"
    API_V1_STR: str = "/api/v1"
    
    # --- Configuración CORS ---
    BACKEND_CORS_ORIGINS: Annotated[
        List[str], BeforeValidator(parse_cors)
    ] = Field(default=["*"])

    # --- Configuración AWS S3 ---
    AWS_S3_BUCKET: str = "mi-bucket-smartsupervision"
    AWS_ACCESS_KEY_ID: str = "test_key"
    AWS_SECRET_ACCESS_KEY: str = "test_secret"

    # --- Constantes de Entidad para la SFC ---
    #TODO: consultar cuales son los valores reales
    SFC_TIPO_ENTIDAD: int = 1
    SFC_ENTIDAD_COD: str = "423"

    # --- Integración con Smart Supervisión (SFC) ---
    SFC_URL_BASE: str = Field(
        default="http://127.0.0.1:8080/",
        description="URL base alias para configuraciones de infraestructura"
    )
    SFC_USERNAME: str = Field(
        default="admin",
        description="Usuario de autenticación asignado por la SFC"
    )
    SFC_PASSWORD: str = Field(
        default="123456789",
        description="Contraseña de autenticación asignada por la SFC"
    )
    SFC_SECRET_KEY: str = Field(
        default="global66_sfc_secret_key_testing_2026",
        description="Llave secreta de firma criptográfica HMAC-SHA256"
    )
    SFC_VERIFY_SIGNATURES: bool = Field(
        default=False,
        description="Interruptor para activar o desactivar la verificación y generación de firmas HMAC en el cliente"
    )
    
    CRM_API_KEY: str = Field(
        default="g66_sk_test_super_secreto_12345", 
        description="API Key requerida para que el CRM consuma este MS"
    )
    CRM_CORS_ORIGINS: List[str] = Field(
        default=["*"], 
        description="Orígenes permitidos para CORS"
    )

settings = Settings()