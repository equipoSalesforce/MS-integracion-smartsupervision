{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ObjectAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Resource": [
        "arn:aws:s3:::global66-crm-b2c-ci-files-766452279030/caso/*",
        "arn:aws:s3:::global66-crm-b2c-ci-files-766452279030/quejas/*"
      ]
    },
    {
      "Sid": "S3ListBucketScoped",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::global66-crm-b2c-ci-files-766452279030",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["caso/*", "quejas/*"]
        }
      }
    }
  ]
}
