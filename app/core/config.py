from typing import List, Any, Optional
from pydantic import BeforeValidator, Field, SecretStr, field_validator
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
    AWS_REGION: str = "us-east-1"
    AWS_ENDPOINT_URL: str = None

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
        description="API Key requerida para que el CRM consuma los endpoints de despacho e integración"
    )
    ADMIN_API_KEY: str = Field(
        default="g66_sk_test_admin_secreto_99999",
        description="API Key administrativa requerida para endpoints de monitoreo e infraestructura (ej. /queue)"
    )
    CRM_CORS_ORIGINS: List[str] = Field(
        default=["*"], 
        description="Orígenes permitidos para CORS"
    )

    # --- 🛠️ Configuración de Cola Centralizada con Redis --- #
    REDIS_HOST: str = Field(
        default="localhost",
        description="Host del servidor Redis para la cola centralizada"
    )
    REDIS_PORT: int = Field(
        default=6379,
        description="Puerto del servidor Redis"
    )
    REDIS_PASSWORD: Optional[str] = Field(
        default=None,
        description="Contraseña de autenticación de Redis (si aplica)"
    )
    REDIS_DB: int = Field(
        default=0,
        description="Número de base de datos de Redis"
    )
    REDIS_SSL: bool = Field(
        default=False,
        description="Activa el cifrado TLS/SSL para la conexión a Redis (ej. AWS ElastiCache)"
    )
    REDIS_URL: Optional[str] = Field(
        default=None,
        description="URL de conexión completa a Redis (opcional, sobrescribe host/port/db)"
    )

    QUEUE_RETRY_INTERVAL_MINUTES: int = Field(default=5)
    QUEUE_MAX_RETRIES: int = Field(default=10)
    QUEUE_ENABLED: bool = Field(default=True)
    QUEUE_RETENTION_DAYS: int = Field(default=7)
    
    # -- Configuración de SMTP alertas -- #
    
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
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REFRESH_TOKEN: Optional[str] = None
    GOOGLE_SPREADSHEET_ID: Optional[str] = None
    GOOGLE_SHEET_RANGE: Optional[str] = None
    GOOGLE_CATALOGS_SPREADSHEET_ID: Optional[str] = None
    
    # --- Control de Throttling (Mini-retries) ---
    SFC_MINI_RETRY_ATTEMPTS: int = Field(
        default=2,
        description="Número de mini-retries inmediatos cuando la SFC responde 429 Throttled/Quota Exceeded"
    )
    SFC_MINI_RETRY_DELAY_SECONDS: float = Field(
        default=5.5,
        description="Pausa en segundos (mini-delay) entre cada mini-retry por throttling"
    )
    
    # --- 🔔 Webhook de Confirmación de Creación hacia el CRM ---
    CRM_WEBHOOK_URL: Optional[str] = Field(
        default=None,
        description="URL del endpoint POST en el CRM para notificar la creación exitosa en la SFC"
    )
    CRM_WEBHOOK_API_KEY: Optional[str] = Field(
        default=None,
        description="API Key enviada en la cabecera X-API-Key hacia el CRM"
    )
    
settings = Settings()