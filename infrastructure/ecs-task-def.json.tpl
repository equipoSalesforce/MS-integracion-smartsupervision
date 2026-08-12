{
  "family": "ms-smartsupervision-${SERVICE_TYPE}-${ENVIRONMENT}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": [
    "FARGATE"
  ],
  "cpu": "${TASK_CPU}",
  "memory": "${TASK_MEMORY}",
  "executionRoleArn": "arn:aws:iam::${AWS_ACCOUNT_ID}:role/ecsTaskExecutionRole",
  "taskRoleArn": "arn:aws:iam::${AWS_ACCOUNT_ID}:role/msSmartsupervisionTaskRole-${ENVIRONMENT}",
  "containerDefinitions": [
    {
      "name": "${SERVICE_TYPE}-service",
      "image": "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/ms-integracion-smartsupervision:${IMAGE_TAG}",
      "essential": true,
      "command": ${CONTAINER_COMMAND},
      "portMappings": ${PORT_MAPPINGS},
      "environment": [
        { "name": "PROJECT_NAME", "value": "MS-integracion-smartsupervision" },
        { "name": "ENVIRONMENT", "value": "${ENVIRONMENT}" },
        { "name": "LOG_LEVEL", "value": "${LOG_LEVEL}" },
        { "name": "RUN_SCHEDULER", "value": "${RUN_SCHEDULER}" },
        { "name": "WEB_CONCURRENCY", "value": "${WEB_CONCURRENCY}" },
        { "name": "AWS_REGION", "value": "${AWS_REGION}" },
        { "name": "AWS_S3_BUCKET", "value": "${AWS_S3_BUCKET}" },
        { "name": "SFC_TIPO_ENTIDAD", "value": "128" },
        { "name": "SFC_ENTIDAD_COD", "value": "6" },
        { "name": "SFC_URL_BASE", "value": "${SFC_URL_BASE}" },
        { "name": "SFC_VERIFY_SIGNATURES", "value": "True" },
        { "name": "REDIS_HOST", "value": "${REDIS_HOST}" },
        { "name": "REDIS_PORT", "value": "6379" },
        { "name": "REDIS_SSL", "value": "True" },
        { "name": "REDIS_CLUSTER_MODE", "value": "False" },
        { "name": "QUEUE_ENABLED", "value": "True" },
        { "name": "QUEUE_RETRY_INTERVAL_MINUTES", "value": "5" },
        { "name": "QUEUE_MAX_RETRIES", "value": "10" },
        { "name": "ALERT_EMAILS_ENABLED", "value": "True" },
        { "name": "SMTP_HOST", "value": "email-smtp.${AWS_REGION}.amazonaws.com" },
        { "name": "SMTP_PORT", "value": "587" },
        { "name": "GOOGLE_SPREADSHEET_ID", "value": "${GOOGLE_SPREADSHEET_ID}" },
        { "name": "GOOGLE_SHEET_RANGE", "value": "MatrizErrores!A:C" },
        { "name": "GOOGLE_CATALOGS_SPREADSHEET_ID", "value": "${GOOGLE_CATALOGS_SPREADSHEET_ID}" }
      ],
      "secrets": [
        {
          "name": "CRM_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:CRM_API_KEY::"
        },
        {
          "name": "ADMIN_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:ADMIN_API_KEY::"
        },
        {
          "name": "SFC_USERNAME",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:SFC_USERNAME::"
        },
        {
          "name": "SFC_PASSWORD",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:SFC_PASSWORD::"
        },
        {
          "name": "SFC_SECRET_KEY",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:SFC_SECRET_KEY::"
        },
        {
          "name": "REDIS_PASSWORD",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:REDIS_PASSWORD::"
        },
        {
          "name": "SMTP_USER",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:SMTP_USER::"
        },
        {
          "name": "SMTP_PASSWORD",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:SMTP_PASSWORD::"
        },
        {
          "name": "ALERT_NOTIFY_EMAILS",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:ALERT_NOTIFY_EMAILS::"
        },
        {
          "name": "CRM_WEBHOOK_URL",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:CRM_WEBHOOK_URL::"
        },
        {
          "name": "CRM_WEBHOOK_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:CRM_WEBHOOK_API_KEY::"
        },
        {
          "name": "GOOGLE_CLIENT_ID",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:GOOGLE_CLIENT_ID::"
        },
        {
          "name": "GOOGLE_CLIENT_SECRET",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:GOOGLE_CLIENT_SECRET::"
        },
        {
          "name": "GOOGLE_REFRESH_TOKEN",
          "valueFrom": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets:GOOGLE_REFRESH_TOKEN::"
        }
      ],
      "healthCheck": {
        "command": [
          "CMD-SHELL",
          "${HEALTHCHECK_CMD}"
        ],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 15
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/ms-smartsupervision-${ENVIRONMENT}-${SERVICE_TYPE}",
          "awslogs-region": "${AWS_REGION}",
          "awslogs-stream-prefix": "${SERVICE_TYPE}"
        }
      }
    }
  ]
}