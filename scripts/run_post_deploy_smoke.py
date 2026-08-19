# scripts/run_post_deploy_smoke.py
"""
Lanza la task efímera del smoke funcional post-deploy (auditoría adversarial v10,
P1-03/P1-05) y espera su resultado.

Corre en el runner de GitHub Actions, usando boto3 directo -- a propósito, en vez
de construir a mano el JSON de --network-configuration/--overrides con bash, que
es frágil para escapar arrays JSON reales (ECS_SUBNET_IDS/ECS_SECURITY_GROUP_IDS
vienen de GitHub Variables como texto JSON). scripts/post_deploy_smoke.py, en
cambio, corre DENTRO de la task efímera, ya en la VPC privada.

La task efímera reutiliza la MISMA imagen y Task Definition que el servicio api
recién desplegado (mismo Task Role, con permiso S3 ya otorgado) -- no requiere
IAM nuevo para la aplicación, sólo permisos adicionales en el rol de deploy OIDC
(ecs:RunTask, ecs:DescribeTasks, elasticloadbalancing:DescribeLoadBalancers).
"""
import json
import os
import sys

import boto3


def main() -> int:
    if len(sys.argv) < 2:
        print("Uso: python scripts/run_post_deploy_smoke.py <environment>")
        return 1

    environment = sys.argv[1].strip().lower()
    region = os.environ.get("AWS_REGION", "us-east-1")
    cluster = f"ms-smartsupervision-{environment}"
    alb_name = f"ms-smartsupervision-{environment}"

    task_definition = os.environ.get("SMOKE_TASK_DEFINITION_ARN", "").strip()
    if not task_definition:
        print("[SMOKE] [FAIL-FAST] SMOKE_TASK_DEFINITION_ARN no está definido.")
        return 1

    try:
        subnets = json.loads(os.environ["ECS_SUBNET_IDS"])
        security_groups = json.loads(os.environ["ECS_SECURITY_GROUP_IDS"])
    except (KeyError, json.JSONDecodeError) as e:
        print(
            "[SMOKE] [FAIL-FAST] ECS_SUBNET_IDS / ECS_SECURITY_GROUP_IDS deben ser "
            f"Variables de GitHub definidas como arrays JSON de strings. Error: {e}"
        )
        return 1

    ecs = boto3.client("ecs", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)

    try:
        alb = elbv2.describe_load_balancers(Names=[alb_name])["LoadBalancers"][0]
    except Exception as e:
        print(
            f"[SMOKE] [FAIL-FAST] No se pudo resolver el ALB '{alb_name}' en {region}. "
            f"¿Ya lo creó Terraform en este ambiente con ese nombre? Error: {e}"
        )
        return 1

    alb_dns = alb["DNSName"]
    print(f"[SMOKE] ALB interno resuelto: {alb_dns}")

    run_response = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_definition,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": security_groups,
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": "api-service",
                    "command": ["python", "scripts/post_deploy_smoke.py"],
                    "environment": [
                        {"name": "SMOKE_ALB_BASE_URL", "value": f"https://{alb_dns}"}
                    ],
                }
            ]
        },
    )
    failures = run_response.get("failures") or []
    if failures:
        print(f"[SMOKE] [FAIL-FAST] run_task falló al lanzar la task: {failures}")
        return 1

    task_arn = run_response["tasks"][0]["taskArn"]
    print(f"[SMOKE] Task efímera lanzada: {task_arn}")

    waiter = ecs.get_waiter("tasks_stopped")
    waiter.wait(cluster=cluster, tasks=[task_arn])

    described = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
    container = described["tasks"][0]["containers"][0]
    exit_code = container.get("exitCode")
    reason = container.get("reason")

    print(f"[SMOKE] Task terminó -- exitCode={exit_code} reason={reason}")
    if exit_code != 0:
        print(
            "[SMOKE] Smoke funcional post-deploy falló (Redis y/o S3 no accesibles "
            "a través de la release recién desplegada)."
        )
        return 1

    print("[SMOKE] Smoke funcional post-deploy OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
