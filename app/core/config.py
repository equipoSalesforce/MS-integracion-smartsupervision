from typing import List, Any, Optional
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

    # --- 🛠️ Configuración de Cola Local (SQLite + APScheduler) ---
    SQLITE_DB_URL: str = Field(
        default="sqlite+aiosqlite:///./cola_local.db",
        description="Cadena de conexión asíncrona para la base de datos SQLite local"
    )
    QUEUE_RETRY_INTERVAL_MINUTES: int = Field(
        default=5,
        description="Frecuencia en minutos con la que el scheduler busca reintentar casos pendientes"
    )
    QUEUE_MAX_RETRIES: int = Field(
        default=10,
        description="Número máximo de reintentos antes de congelar un registro como FALLIDO_DEFINITIVO"
    )
    QUEUE_ENABLED: bool = Field(
        default=True,
        description="Permite habilitar o deshabilitar la ejecución automática del Scheduler de reintentos"
    )
    
    QUEUE_RETENTION_DAYS: int = Field(
        default=7,
        description="Días de retención para registros EXITOSOS en SQLite antes de ser purgados"
    )
    
    # -- Configuración de SMTP para avisar por correo de problemas técnicos -- #
    
    SMTP_HOST: str = Field(default="smtp.gmail.com", description="Servidor SMTP (ej. smtp.gmail.com o smtp.office365.com)")
    SMTP_PORT: int = Field(default=587, description="Puerto TLS estándar (587) o SSL (465)")
    SMTP_USER: str = Field(default="juan.camargo@global66.com", description="Correo remitente del bot")
    SMTP_PASSWORD: str = Field(default="xxxx xxxx xxxx xxxx", description="Contraseña de aplicación de 16 caracteres")
    
    ALERT_NOTIFY_EMAILS: List[str] = Field(
        default=["juan.camargo@global66.com", "tl.correo@global66.com"],
        description="Lista de correos de ingeniería a notificar en fallas de infraestructura"
    )
    ALERT_EMAILS_ENABLED: bool = Field(default=True, description="Switch para activar/desactivar alertas por e-mail")

    # -- Configuración de matriz de errores en sheets -- #
    GOOGLE_SHEETS_MATRIX_URL: Optional[str] = None
    
settings = Settings()