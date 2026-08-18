import json
import os
import re
import sys


def _resolver_secret_suffix(environment: str) -> str:
    """
    🟢 FIX: antes SECRET_SUFFIX era una variable obligatoria que
    un humano tenía que copiar a mano desde la consola de AWS (el sufijo aleatorio de
    6 caracteres que Secrets Manager le agrega al nombre del secreto al crearlo).
    El secreto ya lo crea el plan de Terraform -- no hace falta que nadie rastree
    manualmente su sufijo: se consulta el ARN real directamente en Secrets Manager.

    SECRET_SUFFIX se conserva como override opcional (no obligatorio) sólo para
    testing local/offline sin credenciales AWS reales -- si no se define, se resuelve
    contra el servicio real.
    """
    override = os.getenv("SECRET_SUFFIX")
    if override and override.strip() and override.strip() != "??????":
        return override.strip()

    secret_name = f"{environment.lower()}/smartsupervision/app-secrets"
    try:
        import boto3
        client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1"))
        arn = client.describe_secret(SecretId=secret_name)["ARN"]
        # Los ARNs de Secrets Manager terminan en "-{sufijo de 6 caracteres}"; el
        # nombre del secreto en sí puede contener guiones (ej. "app-secrets"), por eso
        # se parte desde la derecha una sola vez.
        return arn.rsplit("-", 1)[-1]
    except Exception as e:
        raise ValueError(
            f"🚨 [FAIL-FAST] No se pudo resolver el secreto '{secret_name}' en Secrets Manager "
            f"({os.getenv('AWS_REGION', 'us-east-1')}) y no se definió SECRET_SUFFIX como override "
            f"manual. ¿El secreto ya fue creado por el plan de Terraform en este ambiente y el rol "
            f"de deploy tiene permiso secretsmanager:DescribeSecret sobre él? Error original: {e}"
        )


def render_task_definition(service_type: str, environment: str) -> dict:
    is_prod = environment.lower() in ("prod", "production")
    
    # 1. Configuración de dimensiones según ambiente
    cpu = "512" if is_prod else "256"
    memory = "1024" if is_prod else "512"
    log_level = "INFO" if is_prod else "DEBUG"
    web_concurrency = "4" if is_prod else "2"

    # 🟢 FIX HALLAZGO 10: Validación Fail-Fast para AWS_ACCOUNT_ID (Sin fallback ficticio)
    aws_account_id = os.getenv("AWS_ACCOUNT_ID")
    if not aws_account_id or aws_account_id.strip() in ("", "123456789012"):
        raise ValueError(
            "🚨 [FAIL-FAST] La variable de entorno 'AWS_ACCOUNT_ID' es obligatoria y no puede "
            "estar vacía ni usar valores por defecto ficticios (123456789012)."
        )

    # 🟢 FIX HALLAZGO 11 (revisado): el sufijo ya no se exige como variable manual --
    # se resuelve solo contra Secrets Manager (ver _resolver_secret_suffix). Sigue
    # siendo imposible continuar con un sufijo vacío o sin resolver.
    secret_suffix = _resolver_secret_suffix(environment)

    # 🟢 FIX HALLAZGO 45: Validación Fail-Fast para IMAGE_TAG inmutable (Sin fallback a 'latest')
    image_tag = os.getenv("IMAGE_TAG")
    if not image_tag or image_tag.strip().lower() in ("", "latest"):
        raise ValueError(
            "🚨 [FAIL-FAST] La variable de entorno 'IMAGE_TAG' es obligatoria y debe ser un "
            "etiquetado inmutable (ej. Git Commit SHA 'a1b2c3d' o versión semántica 'v1.0.0'). "
            "Se prohíbe el uso de 'latest' como tag de despliegue para garantizar trazabilidad y rollbacks."
        )

    # 🟢 FIX P1-08: Validación Fail-Fast para SMTP_FROM_EMAIL (sin fallback silencioso).
    # En SES, SMTP_USER es una credencial IAM generada, no una dirección entregable — si
    # este render se hace sin SMTP_FROM_EMAIL, la app caería de vuelta a SMTP_USER como
    # remitente (ver app/services/email_service.py) y las alertas fallarían en SES sin
    # ningún aviso hasta que alguien note que dejaron de llegar.
    smtp_from_email = os.getenv("SMTP_FROM_EMAIL")
    if not smtp_from_email or not smtp_from_email.strip():
        raise ValueError(
            "🚨 [FAIL-FAST] La variable de entorno 'SMTP_FROM_EMAIL' es obligatoria para el "
            "despliegue en AWS — debe ser una identidad de remitente verificada en SES, "
            "distinta de la credencial SMTP_USER."
        )

    # 2. Configuración específica según tipo de servicio
    if service_type.lower() == "api":
        run_scheduler = "False"
        container_command = json.dumps([
            "gunicorn", "app.main:app",
            "-k", "uvicorn_worker.UvicornWorker",
            "--bind", "0.0.0.0:8000",
            "--workers", web_concurrency,
            "--timeout", "120",
            "--graceful-timeout", "30"
        ])
        port_mappings = json.dumps([
            {"containerPort": 8000, "hostPort": 8000, "protocol": "tcp"}
        ])
        healthcheck_cmd = "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')\" || exit 1"
        
    elif service_type.lower() == "worker":
        run_scheduler = "True"
        container_command = json.dumps(["python", "-m", "app.worker"])
        port_mappings = json.dumps([])
        healthcheck_cmd = "python /code/infrastructure/healthcheck_worker.py || exit 1"
    else:
        raise ValueError(f"SERVICE_TYPE inválido: '{service_type}'. Debe ser 'api' o 'worker'.")

    # 3. Leer plantilla base
    template_path = os.path.join(os.path.dirname(__file__), "../infrastructure/ecs-task-def.json.tpl")
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 🟢 FIX HALLAZGO 7 (ampliado): cualquier valor insertado dentro de una posición de
    # string JSON ("${...}") debe escaparse igual que HEALTHCHECK_CMD, no sólo ese caso.
    # Variables como CRM_CORS_ORIGINS pueden traer comillas embebidas (ej. un valor con
    # forma de array JSON) y romper el render si se sustituyen como texto plano.
    def _esc(value: str) -> str:
        return json.dumps(value)[1:-1]

    # 4. Mapeo de valores a reemplazar dentro de posiciones de string ("${...}")
    escaped_replacements = {
        "${SERVICE_TYPE}": service_type.lower(),
        "${ENVIRONMENT}": environment.lower(),
        "${TASK_CPU}": cpu,
        "${TASK_MEMORY}": memory,
        "${LOG_LEVEL}": log_level,
        "${WEB_CONCURRENCY}": web_concurrency,
        "${RUN_SCHEDULER}": run_scheduler,
        "${HEALTHCHECK_CMD}": healthcheck_cmd,
        "${AWS_ACCOUNT_ID}": aws_account_id.strip(),
        "${AWS_REGION}": os.getenv("AWS_REGION", "us-east-1"),
        "${IMAGE_TAG}": image_tag.strip(),
        "${AWS_S3_BUCKET}": os.getenv("AWS_S3_BUCKET", f"{environment.lower()}-global66-smartsupervision-attachments"),
        "${SFC_URL_BASE}": os.getenv("SFC_URL_BASE", "https://qasmart.superfinanciera.gov.co"),
        "${CRM_CORS_ORIGINS}": os.getenv("CRM_CORS_ORIGINS", "https://crm.global66.com"),
        "${REDIS_HOST}": os.getenv("REDIS_HOST", f"{environment.lower()}-smartsupervision-redis.cache.amazonaws.com"),
        "${REDIS_SSL}": os.getenv("REDIS_SSL", "True"),
        "${GOOGLE_SPREADSHEET_ID}": os.getenv("GOOGLE_SPREADSHEET_ID", "1a2b3c4d5e6f7g8h9i0j"),
        "${GOOGLE_CATALOGS_SPREADSHEET_ID}": os.getenv("GOOGLE_CATALOGS_SPREADSHEET_ID", "0j9i8h7g6f5e4d3c2b1a"),
        "${SFC_TIPO_ENTIDAD}": os.getenv("SFC_TIPO_ENTIDAD", "128"),
        "${SFC_ENTIDAD_COD}": os.getenv("SFC_ENTIDAD_COD", "6"),
        "${REDIS_CLUSTER_MODE}": os.getenv("REDIS_CLUSTER_MODE", "False"),
        "${SFC_SYNC_MAX_PAGINAS}": os.getenv("SFC_SYNC_MAX_PAGINAS", "1000"),
        "${SFC_SYNC_MAX_SEGUNDOS}": os.getenv("SFC_SYNC_MAX_SEGUNDOS", "300"),
        "${SMTP_FROM_EMAIL}": smtp_from_email.strip()
    }

    # ${CONTAINER_COMMAND} y ${PORT_MAPPINGS} ya son fragmentos JSON completos (arrays) y
    # se insertan SIN comillas circundantes en la plantilla, por lo que NO deben re-escaparse.
    raw_json_replacements = {
        "${CONTAINER_COMMAND}": container_command,
        "${PORT_MAPPINGS}": port_mappings,
    }

    for key, value in escaped_replacements.items():
        content = content.replace(key, _esc(value))
    for key, value in raw_json_replacements.items():
        content = content.replace(key, value)

    # Reemplazar sufijo de secreto (también dentro de posiciones de string JSON)
    content = content.replace("??????", _esc(secret_suffix.strip()))

    # 🟢 FIX HALLAZGO 9: Verificación estricta post-renderizado de cualquier placeholder ${...} no resuelto
    unrendered_placeholders = set(re.findall(r"\$\{[A-Za-z0-9_]+\}", content))
    if unrendered_placeholders:
        raise ValueError(
            f"🚨 [RENDER ERROR] Se detectaron placeholders sin reemplazar en la Task Definition: "
            f"{sorted(list(unrendered_placeholders))}"
        )

    # 🟢 FIX HALLAZGO 11: Verificación estricta post-renderizado de '??????' no resuelto
    if "??????" in content:
        raise ValueError(
            "🚨 [RENDER ERROR] La Task Definition renderizada aún contiene el marcador "
            "de sufijo de secreto '??????' no resuelto."
        )

    # 5. Validar y parsear estrictamente que el resultado sea un JSON válido
    output_json = json.loads(content)
    output_filename = f"ecs-task-def-{service_type.lower()}-{environment.lower()}.json"
    
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(output_json, f, indent=2)

    # Sin emoji: algunas consolas/runners de CI en Windows usan una code page (ej. cp1252)
    # que no puede codificar caracteres Unicode como ✅ y hacía fallar el script aquí mismo.
    print(f"[OK] Renderizada exitosamente la Task Definition: {output_filename}")
    return output_json

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python render_task_def.py <api|worker> <dev|qa|prod>")
        sys.exit(1)
        
    srv_type = sys.argv[1]
    env_name = sys.argv[2]
    render_task_definition(srv_type, env_name)