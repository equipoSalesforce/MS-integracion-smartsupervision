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

def parse_email_list(v: Any) -> List[str]:
    """
    🟢 RESILIENCIA EN SECRETS MANAGER:
    Permite parsear la lista de correos tanto si viene como cadena formateada en JSON
    '["a@g66.com", "b@g66.com"]' como si viene en texto plano separado por comas 'a@g66.com, b@g66.com'.
    """
    if isinstance(v, str):
        v_clean = v.strip()
        if not v_clean.startswith("["):
            return [i.strip() for i in v_clean.split(",") if i.strip()]
    return v

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
    # 🟢 FIX: unificado en un solo campo obligatorio (sin default inseguro).
    # Antes existían BACKEND_CORS_ORIGINS (con default "*", efectivamente en uso
    # por el middleware) y CRM_CORS_ORIGINS (obligatorio, pero nunca conectado al
    # middleware) — dos fuentes de verdad desincronizadas. Se deja una sola.
    CRM_CORS_ORIGINS: Annotated[
        List[str], BeforeValidator(parse_cors)
    ] = Field(description="Orígenes permitidos para CORS (dominios reales del CRM, sin comodín)")

    # --- Configuración AWS S3 ---
    AWS_S3_BUCKET: str
    AWS_ACCESS_KEY_ID: Optional[str] = Field(default=None)
    AWS_SECRET_ACCESS_KEY: Optional[str] = Field(default=None)
    AWS_SESSION_TOKEN: Optional[str] = Field(default=None)  # 🟢 FIX: Necesario para AWS SSO / aws-vault
    AWS_REGION: str
    AWS_ENDPOINT_URL: Optional[str] = None
    AWS_S3_ENDPOINT_URL: Optional[str] = None

    # --- Constantes de Entidad para la SFC ---
    SFC_TIPO_ENTIDAD: int = 128
    SFC_ENTIDAD_COD: str = "6"

    # --- Integración con Smart Supervisión (SFC) ---
    SFC_URL_BASE: str = Field(
        description="URL base alias para configuraciones de infraestructura"
    )
    SFC_USERNAME: str = Field(
        description="Usuario de autenticación asignado por la SFC"
    )
    SFC_PASSWORD: str = Field(
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
        description="API Key requerida para que el CRM consuma los endpoints de despacho e integración"
    )
    ADMIN_API_KEY: str = Field(
        description="API Key administrativa requerida para endpoints de monitoreo e infraestructura (ej. /queue)"
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
    SMTP_USER: str = Field(...)
    SMTP_PASSWORD: str = Field(...)
    
    ALERT_NOTIFY_EMAILS: Annotated[
        List[str], BeforeValidator(parse_email_list)
    ] = Field(..., description="Lista de destinatarios para alertas de infraestructura y DLQ")
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