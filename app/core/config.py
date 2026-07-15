# app/core/config.py
import os
from typing import Optional, List
from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl

class Settings(BaseSettings):
    # Configuración del Microservicio
    PROJECT_NAME: str = "sfc-smartsupervision-integration"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"  # development, qa, production

    # Orígenes permitidos para CORS (por ejemplo, la IP/Dominio de tu CRM local)
    BACKEND_CORS_ORIGINS: List[str] = ["*"]
    
    # Configuración de la API de la SFC (SmartSupervisión)
    SFC_API_BASE_URL: str
    SFC_USERNAME: str
    SFC_PASSWORD: str
    SFC_SECRET_KEY: str
    
    # AWS S3 Configuration
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_REGION: str = "us-east-1"
    AWS_S3_BUCKET: str = "mi-bucket-smartsupervision"

    class Config:
        # Pydantic buscará el archivo .env en la raíz del proyecto
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        
    @classmethod
    def model_rebuild(cls, **kwargs):
        super().model_rebuild(**kwargs)

settings = Settings()