terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

variable "repository" { type = string }
variable "branch" { type = string }
variable "cluster_arn" { type = string }
variable "results_bucket_arn" { type = string }
variable "existing_oidc_provider_arn" {
  type    = string
  default = null
}

locals {
  issuer = "token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.existing_oidc_provider_arn == null ? 1 : 0
  url            = "https://${local.issuer}"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "executor" {
  name                 = "loadtest-github-executor"
  max_session_duration = 7200
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.existing_oidc_provider_arn == null ? aws_iam_openid_connect_provider.github[0].arn : var.existing_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${local.issuer}:aud" = "sts.amazonaws.com"
        "${local.issuer}:sub" = "repo:${var.repository}:ref:refs/heads/${var.branch}"
      } }
    }]
  })
  lifecycle {
    precondition {
      condition     = can(regex("^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", var.repository)) && can(regex("^[a-zA-Z0-9_./-]+$", var.branch))
      error_message = "Repository and branch must be literal identifiers without wildcards."
    }
  }
}

resource "aws_iam_role_policy" "executor" {
  role = aws_iam_role.executor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = var.cluster_arn },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:AbortMultipartUpload"], Resource = "${var.results_bucket_arn}/*" }
    ]
  })
}

output "role_arn" { value = aws_iam_role.executor.arn }
