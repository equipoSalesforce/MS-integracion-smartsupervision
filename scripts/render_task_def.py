# scripts/render_task_def.py
import json
import os
import sys

def render_task_definition(service_type: str, environment: str):
    is_prod = environment.lower() in ("prod", "production")
    
    # 1. Configuración de dimensiones según ambiente
    cpu = "512" if is_prod else "256"
    memory = "1024" if is_prod else "512"
    log_level = "INFO" if is_prod else "DEBUG"
    web_concurrency = "4" if is_prod else "2"
    
    # 2. Configuración específica según tipo de servicio
    if service_type.lower() == "api":
        run_scheduler = "False"
        container_command = json.dumps([
            "gunicorn", "app.main:app",
            "-k", "uvicorn.workers.UvicornWorker",
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

    # 4. Mapeo de valores a reemplazar
    replacements = {
        "${SERVICE_TYPE}": service_type.lower(),
        "${ENVIRONMENT}": environment.lower(),
        "${TASK_CPU}": cpu,
        "${TASK_MEMORY}": memory,
        "${LOG_LEVEL}": log_level,
        "${WEB_CONCURRENCY}": web_concurrency,
        "${RUN_SCHEDULER}": run_scheduler,
        "${CONTAINER_COMMAND}": container_command,
        "${PORT_MAPPINGS}": port_mappings,
        "${HEALTHCHECK_CMD}": healthcheck_cmd,
        "${AWS_ACCOUNT_ID}": os.getenv("AWS_ACCOUNT_ID", "123456789012"),
        "${AWS_REGION}": os.getenv("AWS_REGION", "us-east-1"),
        "${IMAGE_TAG}": os.getenv("IMAGE_TAG", "latest"),
        "${AWS_S3_BUCKET}": os.getenv("AWS_S3_BUCKET", f"{environment.lower()}-global66-smartsupervision-attachments"),
        "${SFC_URL_BASE}": os.getenv("SFC_URL_BASE", "https://qasmart.superfinanciera.gov.co"),
        "${REDIS_HOST}": os.getenv("REDIS_HOST", f"{environment.lower()}-smartsupervision-redis.cache.amazonaws.com"),
        "${REDIS_SSL}": os.getenv("REDIS_SSL", "True"),
        "${GOOGLE_SPREADSHEET_ID}": os.getenv("GOOGLE_SPREADSHEET_ID", "1a2b3c4d5e6f7g8h9i0j"),
        "${GOOGLE_CATALOGS_SPREADSHEET_ID}": os.getenv("GOOGLE_CATALOGS_SPREADSHEET_ID", "0j9i8h7g6f5e4d3c2b1a")
    }

    for key, value in replacements.items():
        content = content.replace(key, value)

    # 5. Validar que el resultado sea un JSON válido antes de guardar
    output_json = json.loads(content)
    output_filename = f"ecs-task-def-{service_type.lower()}-{environment.lower()}.json"
    
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(output_json, f, indent=2)

    print(f"✅ Renderizada exitosamente la Task Definition: {output_filename}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python render_task_def.py <api|worker> <dev|qa|prod>")
        sys.exit(1)
        
    srv_type = sys.argv[1]
    env_name = sys.argv[2]
    render_task_definition(srv_type, env_name)