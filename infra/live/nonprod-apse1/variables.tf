variable "region" {
  type    = string
  default = "ap-southeast-1"
}
variable "cluster_name" {
  type    = string
  default = "loadtest-nonprod-apse1"
}
variable "kubernetes_version" {
  type    = string
  default = "1.36"
}
variable "system_instance_types" {
  type    = list(string)
  default = ["t3.medium"]
}
variable "system_node_count" {
  type    = number
  default = 2
  validation {
    condition     = var.system_node_count >= 2 && var.system_node_count <= 4 && floor(var.system_node_count) == var.system_node_count
    error_message = "Use two to four system nodes."
  }
}
variable "loadtest_instance_types" {
  type    = list(string)
  default = ["c6i.xlarge", "c5.xlarge", "c6a.xlarge", "m6i.xlarge"]
}
variable "loadtest_nodes" {
  description = "Fixed Spot capacity; no node autoscaler is installed."
  type        = number
  default     = 1
  validation {
    condition     = var.loadtest_nodes >= 1 && var.loadtest_nodes <= 10 && floor(var.loadtest_nodes) == var.loadtest_nodes
    error_message = "Spot capacity must be one to ten nodes."
  }
}
variable "existing_github_oidc_provider_arn" {
  description = "Reuse the project's existing GitHub OIDC provider, if present. Null creates it."
  type        = string
  default     = null
}
variable "vpc_id" {
  type = string
}
variable "subnet_ids" {
  type = list(string)
  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "Provide subnets in at least two AZs."
  }
}
variable "subnet_type" {
  description = "Public subnets for the demo, or private subnets with existing NAT/endpoints."
  type        = string
  default     = "private"
  validation {
    condition     = contains(["public", "private"], var.subnet_type)
    error_message = "subnet_type must be public or private."
  }
}
variable "endpoint_public_access_cidrs" {
  description = "Administrator IPv4 /32 addresses allowed to reach the public API. Empty means private-only."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for cidr in var.endpoint_public_access_cidrs : can(cidrnetmask(cidr)) && endswith(cidr, "/32")])
    error_message = "Use explicit administrator IPv4 /32 addresses."
  }
}
variable "github_repository" {
  type = string
  validation {
    condition     = can(regex("^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", var.github_repository))
    error_message = "Use a literal GitHub owner/repository, with no wildcards."
  }
}
variable "github_branch" {
  type    = string
  default = "main"
  validation {
    condition     = can(regex("^[a-zA-Z0-9_./-]+$", var.github_branch))
    error_message = "Use a literal protected branch name."
  }
}
variable "github_environment" {
  description = "Protected GitHub environment that gates the push pipeline; its reviewers approve each load test."
  type        = string
  default     = "loadtest"
}
variable "results_bucket" {
  type = string
}
variable "test_secret_arns" {
  description = "Exact test-only secret ARNs accessible to External Secrets; empty disables secret reads."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.test_secret_arns : can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$", arn))])
    error_message = "Only exact Secrets Manager ARNs, no wildcards."
  }
}
variable "monthly_budget_usd" {
  type    = number
  default = 200
  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "Budget must be positive."
  }
}
variable "budget_emails" {
  type    = list(string)
  default = []
}
