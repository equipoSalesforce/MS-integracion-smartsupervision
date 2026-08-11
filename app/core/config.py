# app/core/config.py
from typing import List, Any, Optional
from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
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
    AWS_ACCESS_KEY_ID: Optional[str] = Field(default="test_key")
    AWS_SECRET_ACCESS_KEY: Optional[str] = Field(default="test_secret")
    AWS_REGION: str = "us-east-1"
    AWS_ENDPOINT_URL: Optional[str] = None

    # --- Constantes de Entidad para la SFC ---
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

    # --- Configuración de Cola Centralizada con Redis ---
    REDIS_HOST: str = Field(default="localhost")
    REDIS_PORT: int = Field(default=6379)
    REDIS_PASSWORD: Optional[str] = Field(default=None)
    REDIS_DB: int = Field(default=0)
    REDIS_SSL: bool = Field(default=False)
    REDIS_URL: Optional[str] = Field(default=None)

    QUEUE_RETRY_INTERVAL_MINUTES: int = Field(default=5)
    QUEUE_MAX_RETRIES: int = Field(default=10)
    QUEUE_ENABLED: bool = Field(default=True)
    QUEUE_RETENTION_DAYS: int = Field(default=7)
    QUEUE_RETENTION_DAYS_DLQ: int = Field(default=30)
    
    # --- Configuración SMTP Alertas ---
    SMTP_HOST: str = Field(default="smtp.gmail.com")
    SMTP_PORT: int = Field(default=587)
    SMTP_USER: str = Field(default="juan.camargo@global66.com")
    SMTP_PASSWORD: str = Field(default="xxxx xxxx xxxx xxxx")
    
    ALERT_NOTIFY_EMAILS: List[str] = Field(
        default=["juan.camargo@global66.com", "tl.correo@global66.com"]
    )
    ALERT_EMAILS_ENABLED: bool = Field(default=True)

    # --- Configuración de Matriz en Sheets ---
    GOOGLE_SHEETS_MATRIX_URL: Optional[str] = None
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REFRESH_TOKEN: Optional[str] = None
    GOOGLE_SPREADSHEET_ID: Optional[str] = None
    GOOGLE_SHEET_RANGE: Optional[str] = None
    GOOGLE_CATALOGS_SPREADSHEET_ID: Optional[str] = None
    
    # --- Control de Throttling ---
    SFC_MINI_RETRY_ATTEMPTS: int = Field(default=2)
    SFC_MINI_RETRY_DELAY_SECONDS: float = Field(default=5.5)
    
    # --- Webhook CRM ---
    CRM_WEBHOOK_URL: Optional[str] = Field(default=None)
    CRM_WEBHOOK_API_KEY: Optional[str] = Field(default=None)
    
    RUN_SCHEDULER: bool = Field(default=False)

    @model_validator(mode="after")
    def validar_secretos_produccion(self):
        """
        🛡️ VALIDACIÓN STRICT FAIL-FAST EN ARRANQUE
        Impide la ejecución en ambientes productivos/no-locales si no se inyectaron
        secretos reales desde AWS Secrets Manager / Environment.
        """
        env_lower = (self.ENVIRONMENT or "").strip().lower()
        ambientes_estrictos = ("production", "prod", "staging", "qa")

        if env_lower in ambientes_estrictos:
            valores_inseguros_prohibidos = {
                "CRM_API_KEY": ["g66_sk_test_super_secreto_12345", "test", "12345"],
                "ADMIN_API_KEY": ["g66_sk_test_admin_secreto_99999", "admin", "12345"],
                "SFC_SECRET_KEY": ["global66_sfc_secret_key_testing_2026", "secret", "test"],
                "SFC_PASSWORD": ["123456789", "123456", "admin", "password"]
            }

            campos_comprometidos = []
            for campo, valores_inseguros in valores_inseguros_prohibidos.items():
                valor_actual = getattr(self, campo, None)
                if not valor_actual or str(valor_actual).strip() in valores_inseguros:
                    campos_comprometidos.append(campo)

            if campos_comprometidos:
                lista_campos_str = ", ".join(campos_comprometidos)
                raise ValueError(
                    f"🚨 [RIESGO CRÍTICO DE SEGURIDAD] El microservicio arrancó en ambiente '{self.ENVIRONMENT}' "
                    f"pero detectó valores por defecto/inseguros en los campos: [{lista_campos_str}]. "
                    f"Asegúrese de inyectar los secretos reales desde AWS Secrets Manager antes de desplegar en ECS."
                )

        return self

settings = Settings()