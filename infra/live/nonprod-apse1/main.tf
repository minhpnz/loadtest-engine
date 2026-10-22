locals {
  name   = var.cluster_name
  limits = jsondecode(file("${path.module}/../../limits.tfvars.json")).limits
}

data "aws_caller_identity" "current" {}
data "aws_vpc" "existing" { id = var.vpc_id }
data "aws_subnet" "selected" {
  for_each = toset(var.subnet_ids)
  id       = each.value
}

resource "terraform_data" "validate_inputs" {
  lifecycle {
    precondition {
      condition     = local.limits.max_vus <= 5000 && local.limits.max_duration_seconds <= 2700 && local.limits.max_environment_seconds <= 3600 && local.limits.max_concurrent_runs == 1
      error_message = "Platform limits exceed approved ceilings."
    }
    precondition {
      condition     = alltrue([for subnet in data.aws_subnet.selected : subnet.vpc_id == var.vpc_id && subnet.map_public_ip_on_launch == (var.subnet_type == "public")]) && length(toset([for subnet in data.aws_subnet.selected : subnet.availability_zone])) >= 2
      error_message = "Provide subnets from this VPC in at least two AZs. Public demo subnets must auto-assign public IPs; private subnets must not. Also review their route tables."
    }
    precondition {
      condition     = data.aws_vpc.existing.enable_dns_support && data.aws_vpc.existing.enable_dns_hostnames
      error_message = "The existing VPC must enable DNS support and DNS hostnames."
    }
  }
}

module "github_role" {
  source                     = "../../modules/github-oidc-role"
  repository                 = var.github_repository
  branch                     = var.github_branch
  cluster_arn                = "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/${local.name}"
  results_bucket_arn         = module.results.bucket_arn
  existing_oidc_provider_arn = var.existing_github_oidc_provider_arn
}

module "cluster" {
  source = "../../modules/eks-loadtest-cluster"

  name                         = local.name
  kubernetes_version           = var.kubernetes_version
  vpc_id                       = var.vpc_id
  subnet_ids                   = var.subnet_ids
  ci_role_arn                  = module.github_role.role_arn
  ci_kubernetes_group          = "loadtest-engine-executor"
  endpoint_public_access_cidrs = var.endpoint_public_access_cidrs
  enable_network_policy        = true
  system_node_count            = var.system_node_count
  system_instance_types        = var.system_instance_types
  loadtest_instance_types      = var.loadtest_instance_types
  loadtest_max_nodes           = var.loadtest_nodes
  loadtest_desired_nodes       = var.loadtest_nodes
  # No module-level depends_on: it defers every data source inside the module to apply time
  # and breaks count/for_each. The preconditions above still fail the plan on bad inputs.
}

module "results" {
  source         = "../../modules/results-bucket"
  bucket_name    = var.results_bucket
  retention_days = local.limits.retention_days
}

resource "aws_iam_role" "external_secrets" {
  name = "loadtest-external-secrets"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"] }]
  })
}

resource "aws_iam_role_policy" "test_secrets" {
  count = length(var.test_secret_arns) > 0 ? 1 : 0
  role  = aws_iam_role.external_secrets.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"], Resource = var.test_secret_arns }]
  })
}

resource "aws_eks_pod_identity_association" "external_secrets" {
  cluster_name    = module.cluster.cluster_name
  namespace       = "external-secrets"
  service_account = "external-secrets"
  role_arn        = aws_iam_role.external_secrets.arn
}

resource "aws_budgets_budget" "platform" {
  name         = "loadtest-platform-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$loadtest-engine"]
  }
  dynamic "notification" {
    for_each = length(var.budget_emails) > 0 ? [80, 100] : []
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.budget_emails
    }
  }
}
