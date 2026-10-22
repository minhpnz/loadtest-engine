output "bucket_name" {
  description = "Results bucket name."
  value       = aws_s3_bucket.this.id
}

output "bucket_arn" {
  description = "Results bucket ARN (grant writers s3:PutObject on <arn>/<prefix>/*)."
  value       = aws_s3_bucket.this.arn
}
