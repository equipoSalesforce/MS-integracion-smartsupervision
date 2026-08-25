# app/core/config.py
import json
from typing import ClassVar, FrozenSet, List, Any, Optional, Union
from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Annotated

# 🟢 FIX (revisión despliegue AWS): valores reconocidos de ENVIRONMENT. Cualquier
# otro valor (typo en la Task Definition, variable mal resuelta, etc.) debe fallar
# el arranque explícitamente -- ver validar_environment_conocido más abajo. Antes,
# un valor no reconocido caía silenciosamente en la rama permisiva de
# validar_configuracion_estricta (que sólo aplica sobre un tuple fijo de nombres
# "conocidos"), permitiendo que el microservicio arrancara en producción con
# secretos/CORS/URLs de ejemplo sin ningún error.
ENVIRONMENTS_RECONOCIDOS = ("local", "development", "test", "ci", "dev", "qa", "staging", "prod", "production")

def parse_cors(v: Any) -> List[str]:
    """
    🟢 PARSER RESILIENTE DE CORS:
    Soporta arreglos JSON '["*"]', listas Python, o cadenas separadas por comas '*', 'http://a.com,http://b.com'.
    """
    if isinstance(v, str):
        v_clean = v.strip()
        if v_clean.startswith("[") and v_clean.endswith("]"):
            try:
                parsed = json.loads(v_clean)
                if isinstance(parsed, list):
                    return [str(i).strip() for i in parsed if str(i).strip()]
            except Exception:
                # No es JSON válido: se ignora a propósito y se cae al parseo por comas de abajo.
                pass
        return [i.strip() for i in v_clean.split(",") if i.strip()]
    elif isinstance(v, list):
        return [str(i).strip() for i in v if str(i).strip()]
    raise ValueError(f"Valor CORS inválido: {v}")

def parse_email_list(v: Any) -> List[str]:
    """
    🟢 PARSER RESILIENTE DE EMAILS:
    Soporta formato JSON '["a@g66.com"]' o texto separado por comas 'a@g66.com, b@g66.com'.
    """
    if isinstance(v, str):
        v_clean = v.strip()
        if v_clean.startswith("[") and v_clean.endswith("]"):
            try:
                parsed = json.loads(v_clean)
                if isinstance(parsed, list):
                    return [str(i).strip() for i in parsed if str(i).strip()]
            except Exception:
                # No es JSON válido: se ignora a propósito y se cae al parseo por comas de abajo.
                pass
        return [i.strip() for i in v_clean.split(",") if i.strip()]
    elif isinstance(v, list):
        return [str(i).strip() for i in v if str(i).strip()]
    raise ValueError(f"Lista de emails inválida: {v}")

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

    @field_validator("ENVIRONMENT")
    @classmethod
    def validar_environment_conocido(cls, v: str) -> str:
        v_norm = (v or "").strip().lower()
        if v_norm not in ENVIRONMENTS_RECONOCIDOS:
            raise ValueError(
                f"🚨 [FAIL-FAST] ENVIRONMENT='{v}' no es un valor reconocido. Debe ser uno de: "
                f"{', '.join(ENVIRONMENTS_RECONOCIDOS)}. Un valor no reconocido antes caía "
                f"silenciosamente en el ambiente permisivo 'local', saltándose toda la "
                f"validación estricta de secretos/CORS/URLs."
            )
        return v

    # 🟢 FIX HALLAZGO 28: Deshabilitar Swagger/OpenAPI por defecto en entornos no-locales
    ENABLE_DOCS: bool = Field(
        default=False, 
        description="Interruptor de seguridad para activar o desactivar /docs, /redoc y /openapi.json"
    )
    
    ENABLE_FILE_LOGS: bool = Field(
        default=False,
        description="Interruptor para configurar si se muestra o no logs de archivos binarios"
    )

    # --- Configuración CORS ---
    CRM_CORS_ORIGINS: Annotated[
        Union[List[str], str], BeforeValidator(parse_cors)
    ] = Field(..., description="Orígenes permitidos para CORS (dominios reales del CRM, sin comodín)")

    # 🟢 FIX HALLAZGO 41: Límite global de tamaño de request body. La API sólo recibe
    # metadatos JSON (los archivos viajan por S3, nunca como bytes en el body), por lo
    # que un límite generoso en MB es suficiente y acota el abuso/consumo de memoria.
    MAX_REQUEST_BODY_SIZE_BYTES: int = Field(
        default=2 * 1024 * 1024,
        description="Tamaño máximo permitido (en bytes) para el body de una request HTTP entrante."
    )

    # --- Configuración AWS S3 ---
    AWS_S3_BUCKET: str = Field(..., description="Nombre del bucket S3 para adjuntos")
    AWS_ACCESS_KEY_ID: Optional[str] = Field(default=None)
    AWS_SECRET_ACCESS_KEY: Optional[str] = Field(default=None)
    AWS_SESSION_TOKEN: Optional[str] = Field(default=None)
    AWS_REGION: str = Field(..., description="Región principal de AWS (ej. us-east-1)")
    AWS_ENDPOINT_URL: Optional[str] = None
    AWS_S3_ENDPOINT_URL: Optional[str] = None

    # --- Constantes de Entidad para la SFC ---
    SFC_TIPO_ENTIDAD: int = 128
    SFC_ENTIDAD_COD: str = "6"

    # --- Integración con Smart Supervisión (SFC) ---
    SFC_URL_BASE: str = Field(..., description="URL base de la plataforma Smart Supervisión de la SFC")
    SFC_USERNAME: str = Field(..., description="Usuario de autenticación asignado por la SFC")
    SFC_PASSWORD: str = Field(..., description="Contraseña de autenticación asignada por la SFC")
    
    SFC_SECRET_KEY: str = Field(
        ...,
        description="Llave secreta de firma criptográfica HMAC-SHA256 (Obligatoria sin defaults)"
    )
    
    CRM_API_KEY: str = Field(..., description="API Key requerida para consumos del CRM")
    ADMIN_API_KEY: str = Field(..., description="API Key administrativa requerida para monitoreo")

    # --- Configuración de Cola Centralizada con Redis ---
    REDIS_HOST: str = Field(default="localhost")
    REDIS_PORT: int = Field(default=6379)
    REDIS_PASSWORD: Optional[str] = Field(default=None)
    REDIS_DB: int = Field(default=0)
    REDIS_SSL: bool = Field(default=False)
    # 🟢 FIX HALLAZGO 51: Declaración formal del campo REDIS_CLUSTER_MODE en Settings
    REDIS_CLUSTER_MODE: bool = Field(
        default=False, 
        description="Activa el modo Redis Cluster para integración con AWS ElastiCache Cluster"
    )
    REDIS_URL: Optional[str] = Field(default=None)

    QUEUE_RETRY_INTERVAL_MINUTES: int = Field(default=5)
    QUEUE_MAX_RETRIES: int = Field(default=10)
    QUEUE_ENABLED: bool = Field(default=True)
    QUEUE_RETENTION_DAYS: int = Field(default=7)
    QUEUE_RETENTION_DAYS_DLQ: int = Field(default=30)

    # 🟢 FIX P1-12: los flujos de paginación de M1/M4 seguían el enlace "next" de la
    # SFC en un `while True` sin cota. Un enlace de paginación defectuoso (ciclo,
    # bug de la SFC) o un backlog anómalamente grande podían dejar el request
    # colgado indefinidamente y acumulando resultados en memoria sin límite.
    SFC_SYNC_MAX_PAGINAS: int = Field(
        default=1000,
        description="Máximo de páginas a seguir en un ciclo de paginación de la SFC (M1/M4) antes de cortar y alertar."
    )
    SFC_SYNC_MAX_SEGUNDOS: int = Field(
        default=300,
        description="Tiempo máximo (segundos) que un ciclo de paginación de la SFC (M1/M4) puede ejecutarse antes de cortar y alertar."
    )

    # --- Configuración SMTP Alertas ---
    SMTP_HOST: str = Field(default="smtp.gmail.com")
    SMTP_PORT: int = Field(default=587)
    SMTP_USER: str = Field(...)
    SMTP_PASSWORD: str = Field(...)
    # 🟢 FIX P1-08: en SES, SMTP_USER es una credencial IAM generada, no una dirección de
    # correo entregable — reutilizarla como remitente puede fallar la entrega/DMARC. Se
    # separa el remitente visible; por defecto usa SMTP_USER para no romper entornos que
    # ya usan un proveedor (ej. Gmail) donde el usuario SÍ es una dirección válida.
    SMTP_FROM_EMAIL: Optional[str] = Field(
        default=None,
        description="Dirección 'From' para alertas por correo. Si no se define, usa SMTP_USER (válido para Gmail; en SES debe configurarse explícitamente con una identidad verificada)."
    )
    
    ALERT_NOTIFY_EMAILS: Annotated[
        Union[List[str], str], BeforeValidator(parse_email_list)
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
    # 🟢 FIX (auditoría adversarial v10, P1-06): CRM_WEBHOOK_URL exige https:// por
    # defecto en ambientes desplegables -- este flag es la única forma de permitir
    # http:// explícitamente, y sólo debe declararse si el webhook es un endpoint
    # interno de confianza (misma VPC/cuenta AWS, sin cruzar a internet). SFC_URL_BASE
    # no tiene un flag equivalente a propósito: es el endpoint público de un ente
    # regulador financiero, no existe un escenario legítimo de excepción para él.
    CRM_WEBHOOK_ALLOW_INSECURE_HTTP: bool = Field(
        default=False,
        description="Permite http:// sin cifrar para CRM_WEBHOOK_URL. Declarar sólo si "
        "el webhook es un endpoint interno de confianza; nunca para un destino externo."
    )
    
    RUN_SCHEDULER: bool = Field(default=False)

    # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): la lista negra sólo atrapa
    # los valores literales conocidos -- un secreto nuevo pero débil (ej.
    # CRM_API_KEY="abc") la pasa sin problema. Se exige un mínimo de longitud para
    # los secretos que ESTE servicio genera/controla (API keys propias, clave HMAC
    # de firma) -- no cubre entropía real, pero cierra el caso genérico de "valor
    # corto de prueba que nadie pensó en agregar a la lista negra".
    LONGITUD_MINIMA_SECRETO_PROPIO: ClassVar[int] = 16
    CAMPOS_CON_MINIMO_DE_LONGITUD: ClassVar[FrozenSet[str]] = frozenset({"CRM_API_KEY", "ADMIN_API_KEY", "SFC_SECRET_KEY"})

    def _validar_secretos_inseguros(self) -> List[str]:
        """1. Auditoría de Secretos e API Keys inseguros o de ejemplo."""
        errores = []
        valores_inseguros_prohibidos = {
            "CRM_API_KEY": ["g66_sk_test_super_secreto_12345", "test", "12345", "secret"],
            "ADMIN_API_KEY": ["g66_sk_test_admin_secreto_99999", "admin", "12345", "secret"],
            "SFC_SECRET_KEY": ["global66_sfc_secret_key_testing_2026", "secret", "test", "12345"],
            # 🟡 SFC_PASSWORD queda fuera del mínimo de longitud: la asigna la SFC
            # (no la generamos nosotros), así que no controlamos su longitud real.
            "SFC_PASSWORD": ["123456789", "123456", "admin", "password", "test"]
        }

        for campo, valores_inseguros in valores_inseguros_prohibidos.items():
            valor_actual = getattr(self, campo, None)
            valor_str = str(valor_actual).strip() if valor_actual else ""

            if not valor_str or valor_str in valores_inseguros:
                errores.append(f"- Campo '{campo}' contiene un valor inseguro o por defecto de prueba.")
            elif campo in self.CAMPOS_CON_MINIMO_DE_LONGITUD and len(valor_str) < self.LONGITUD_MINIMA_SECRETO_PROPIO:
                errores.append(
                    f"- Campo '{campo}' tiene {len(valor_str)} caracteres -- por debajo del mínimo de "
                    f"{self.LONGITUD_MINIMA_SECRETO_PROPIO} exigido para un secreto que este servicio controla."
                )

        return errores

    def _validar_sfc_url_base(self, env_lower: str) -> List[str]:
        """
        2. Validación de URL Base de la SFC.
        🟢 FIX (auditoría adversarial v10, P1-06): exige https:// -- SFC_URL_BASE
        es el endpoint público de un ente regulador financiero (Superintendencia
        Financiera de Colombia), nunca un destino interno; no existe razón legítima
        para permitir http:// sin cifrar aquí.
        """
        sfc_url = (self.SFC_URL_BASE or "").strip().lower()
        if not sfc_url.startswith("https://") or "example.com" in sfc_url or "localhost" in sfc_url:
            return [
                f"- Campo 'SFC_URL_BASE' ('{self.SFC_URL_BASE}') debe ser una URL https:// "
                f"válida para {env_lower} (endpoint externo de la SFC; no se acepta http:// sin cifrar)."
            ]
        return []

    def _validar_cors_wildcard(self, env_lower: str) -> List[str]:
        """3. Prohibición estricta del comodín '*' en CRM_CORS_ORIGINS en ambientes desplegables/CI."""
        cors_origins = self.CRM_CORS_ORIGINS if isinstance(self.CRM_CORS_ORIGINS, list) else [self.CRM_CORS_ORIGINS]
        if any(o.strip() == "*" or "*" in o for o in cors_origins):
            return [
                f"- Campo 'CRM_CORS_ORIGINS' ({cors_origins}) contiene el comodín '*'. "
                f"Se requieren orígenes HTTPS/HTTP explícitos (ej. 'https://crm.global66.com') en ambiente '{env_lower}'."
            ]
        return []

    def _validar_webhook_crm(self, env_lower: str) -> List[str]:
        """
        4. Validación de URL y API Key del Webhook del CRM.
        🟢 FIX (auditoría adversarial v10, P1-06): exige https:// por defecto --
        http:// sólo se acepta si CRM_WEBHOOK_ALLOW_INSECURE_HTTP=true lo declara
        explícitamente (para el caso legítimo de un webhook interno a la VPC/cuenta
        AWS). El default sigue siendo seguro; nadie hereda permisividad sin pedirla.
        """
        errores = []
        webhook_url = (self.CRM_WEBHOOK_URL or "").strip().lower()
        if not webhook_url or "example.com" in webhook_url:
            errores.append(
                f"- Campo 'CRM_WEBHOOK_URL' ('{self.CRM_WEBHOOK_URL}') es obligatorio y debe ser "
                f"una URL HTTPS válida en ambiente '{env_lower}'."
            )
        elif webhook_url.startswith("http://") and not self.CRM_WEBHOOK_ALLOW_INSECURE_HTTP:
            errores.append(
                f"- Campo 'CRM_WEBHOOK_URL' ('{self.CRM_WEBHOOK_URL}') usa http:// sin cifrar en "
                f"ambiente '{env_lower}'. Si es un endpoint interno de confianza, declárelo "
                f"explícitamente con CRM_WEBHOOK_ALLOW_INSECURE_HTTP=true; si no, use https://."
            )
        elif not webhook_url.startswith(("http://", "https://")):
            errores.append(
                f"- Campo 'CRM_WEBHOOK_URL' ('{self.CRM_WEBHOOK_URL}') no es una URL http(s) "
                f"válida en ambiente '{env_lower}'."
            )
        # 🟢 FIX (revisión despliegue AWS): CRM_WEBHOOK_API_KEY no se validaba aquí --
        # si faltaba, crm_webhook_service.py enviaba "X-API-Key": "" en silencio en
        # vez de fallar el arranque.
        if not (self.CRM_WEBHOOK_API_KEY or "").strip():
            errores.append(
                f"- Campo 'CRM_WEBHOOK_API_KEY' es obligatorio en ambiente '{env_lower}' "
                f"(sin él, las notificaciones al CRM se envían sin autenticar)."
            )
        return errores

    def _validar_redis_produccion(self, env_lower: str) -> List[str]:
        """
        🟢 FIX (revisión despliegue AWS): REDIS_PASSWORD/REDIS_SSL no se validaban
        aquí -- un secreto de Redis mal resuelto en Secrets Manager caía en silencio
        a una conexión sin autenticar/sin TLS contra ElastiCache en vez de fallar el
        arranque. Se excluye 'dev' de este bloque a propósito: docker-compose.yml
        reutiliza ENVIRONMENT=dev para el Redis local sin auth/TLS de
        infrastructure/docker-compose.yml (contenedor redis:7-alpine sin
        --requirepass), que es un uso legítimo y distinto del ambiente AWS "dev" real
        (ese sí pasa por ecs-task-def.json.tpl, que ya inyecta REDIS_PASSWORD desde
        Secrets Manager y REDIS_SSL=True por defecto).
        """
        ambientes_redis_real = ("production", "prod", "staging", "qa", "ci")
        if env_lower not in ambientes_redis_real:
            return []

        errores = []
        if not (self.REDIS_PASSWORD or "").strip():
            errores.append(
                f"- Campo 'REDIS_PASSWORD' es obligatorio en ambiente '{env_lower}' "
                f"(sin él, la conexión a ElastiCache queda sin autenticar)."
            )
        if not self.REDIS_SSL:
            errores.append(
                f"- Campo 'REDIS_SSL' debe estar en True en ambiente '{env_lower}' "
                f"para cifrar en tránsito la conexión a ElastiCache."
            )
        # 🟢 FIX (auditoría adversarial v10, P1-04 residual): render_task_def.py
        # ya no puede producir un REDIS_HOST vacío (lo resuelve contra ElastiCache
        # o falla), pero Settings seguía sin defensa propia -- con
        # env_ignore_empty=True, un REDIS_HOST vacío/no inyectado (override manual
        # en la consola de ECS, bypass del renderer, etc.) caía en silencio al
        # default "localhost" en vez de fallar el arranque.
        redis_host = (self.REDIS_HOST or "").strip().lower()
        if not redis_host or redis_host in ("localhost", "127.0.0.1"):
            errores.append(
                f"- Campo 'REDIS_HOST' ('{self.REDIS_HOST}') no es válido en ambiente "
                f"'{env_lower}' (vacío o localhost cae en silencio al Redis por defecto "
                f"en vez de conectar contra ElastiCache real)."
            )
        return errores

    def _validar_docs_expuestos(self, env_lower: str) -> List[str]:
        """5. Bloquear exposición accidental de Swagger/ReDoc en Producción / Staging."""
        if env_lower in ("production", "prod", "staging") and self.ENABLE_DOCS:
            return [
                f"- Campo 'ENABLE_DOCS' está activado en entorno '{env_lower}'. La documentación interactiva "
                "Swagger/ReDoc debe permanecer deshabilitada en ambientes de producción."
            ]
        return []

    @model_validator(mode="after")
    def validar_configuracion_estricta(self):
        """
        🛡️ VALIDACIÓN STRICT FAIL-FAST EN ARRANQUE
        Aplica validaciones estrictas en entornos desplegables (incluyendo CI, Dev, QA, Staging y Prod)
        para impedir el arranque con valores inseguros, URLs ficticias o wildcard CORS (*).
        """
        env_lower = (self.ENVIRONMENT or "").strip().lower()
        ambientes_estrictos = ("production", "prod", "staging", "qa", "dev", "ci")

        if env_lower not in ambientes_estrictos:
            return self

        errores_validacion = [
            *self._validar_secretos_inseguros(),
            *self._validar_sfc_url_base(env_lower),
            *self._validar_cors_wildcard(env_lower),
            *self._validar_webhook_crm(env_lower),
            *self._validar_redis_produccion(env_lower),
            *self._validar_docs_expuestos(env_lower),
        ]

        if errores_validacion:
            lista_errores_str = "\n".join(errores_validacion)
            raise ValueError(
                f"🚨 [RIESGO CRÍTICO DE SEGURIDAD EN AMBIENTE '{self.ENVIRONMENT.upper()}']\n"
                f"El microservicio canceló su arranque debido a fallos de configuración:\n"
                f"{lista_errores_str}\n"
                f"Asegúrese de inyectar variables de entorno reales antes de continuar."
            )

        return self

settings = Settings()