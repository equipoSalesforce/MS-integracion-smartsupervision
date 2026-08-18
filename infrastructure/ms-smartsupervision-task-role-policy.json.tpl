{
  "Version": "2012-10-17",
  "Statement": [
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
    }
  ]
}
