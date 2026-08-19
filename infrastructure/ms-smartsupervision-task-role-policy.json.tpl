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
        "arn:aws:s3:::${AWS_S3_BUCKET}",
        "arn:aws:s3:::${AWS_S3_BUCKET}/*"
      ]
    }
  ]
}
