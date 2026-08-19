# scripts/post_deploy_smoke.py
"""
Smoke funcional post-deploy (auditoría adversarial v10, P1-03/P1-05).

`aws ecs wait services-stable` sólo prueba que ECS cree que las tasks están
sanas (o que /health/live responde) -- no que el flujo de negocio funcione.
Este script corre DENTRO de una task efímera lanzada por el job `deploy`
(misma imagen, mismo Task Role que el servicio api ya desplegado, en la misma
VPC privada) y valida dos dependencias reales sin sobrecargar /health/ready
con chequeos que no le corresponden a un liveness/readiness:

  - Redis: pegándole a /health/ready A TRAVÉS del ALB interno -- de punta a
    punta (routing + target group + contenedor + Redis), no sólo el contenedor.
  - S3: head_bucket contra el bucket real del ambiente, usando las credenciales
    del propio Task Role -- prueba el permiso IAM real, no lo asume.

Login SFC queda fuera de este alcance a propósito (ver discusión P1-05).
"""
import json
import os
import sys

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError


def verificar_redis_via_alb(base_url: str, timeout: float = 10.0) -> bool:
    url = f"{base_url.rstrip('/')}/health/ready"
    try:
        resp = httpx.get(url, timeout=timeout)
    except Exception as e:
        print(f"[SMOKE][Redis] ERROR al llamar {url}: {e}")
        return False

    ok = resp.status_code == 200
    print(f"[SMOKE][Redis] GET {url} -> {resp.status_code} {'OK' if ok else 'FAIL'}")
    if not ok:
        print(f"[SMOKE][Redis] body: {resp.text[:500]}")
    return ok


def verificar_s3(bucket: str, region: str) -> bool:
    try:
        client = boto3.client("s3", region_name=region)
        client.head_bucket(Bucket=bucket)
    except (ClientError, BotoCoreError) as e:
        print(f"[SMOKE][S3] ERROR head_bucket({bucket}): {e}")
        return False

    print(f"[SMOKE][S3] head_bucket({bucket}) -> OK")
    return True


def main() -> int:
    base_url = os.environ.get("SMOKE_ALB_BASE_URL", "").strip()
    bucket = os.environ.get("AWS_S3_BUCKET", "").strip()
    region = os.environ.get("AWS_REGION", "us-east-1").strip()

    # Sin emoji: algunas consolas/runners de CI en Windows usan una code page (ej.
    # cp1252) que no puede codificar caracteres Unicode y hace fallar el script aquí
    # mismo (mismo motivo por el que scripts/render_task_def.py ya los evita).
    if not base_url:
        print("[SMOKE] [FAIL-FAST] SMOKE_ALB_BASE_URL no está definido.")
        return 1
    if not bucket:
        print("[SMOKE] [FAIL-FAST] AWS_S3_BUCKET no está definido.")
        return 1

    resultados = {
        "redis": verificar_redis_via_alb(base_url),
        "s3": verificar_s3(bucket, region),
    }
    print(f"[SMOKE] Resultado: {json.dumps(resultados)}")
    return 0 if all(resultados.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
