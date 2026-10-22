mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  bucket_name    = "acme-loadtest-results-nonprod"
  retention_days = 30
}

run "public_access_fully_blocked" {
  command = apply

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.this.block_public_acls,
      aws_s3_bucket_public_access_block.this.block_public_policy,
      aws_s3_bucket_public_access_block.this.ignore_public_acls,
      aws_s3_bucket_public_access_block.this.restrict_public_buckets,
    ])
    error_message = "All four public access block flags must be true."
  }

  assert {
    condition     = aws_s3_bucket_lifecycle_configuration.this.rule[0].expiration[0].days == 30
    error_message = "Lifecycle expiry must equal retention_days."
  }
}

run "zero_retention_rejected" {
  command = plan

  variables {
    retention_days = 0
  }

  expect_failures = [var.retention_days]
}
