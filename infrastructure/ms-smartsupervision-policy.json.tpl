{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SecretsManagerAccess",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": [
        "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${ENVIRONMENT}/smartsupervision/app-secrets-*"
      ]
    },
    {
      "Sid": "S3BucketAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::${ENVIRONMENT}-global66-smartsupervision-attachments",
        "arn:aws:s3:::${ENVIRONMENT}-global66-smartsupervision-attachments/*"
      ]
    },
    {
      "Sid": "CloudWatchLogsAccess",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": [
        "arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/ecs/ms-smartsupervision-${ENVIRONMENT}-*"
      ]
    }
  ]
}