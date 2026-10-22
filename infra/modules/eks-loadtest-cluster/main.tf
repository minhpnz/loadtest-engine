module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.29.0" # exact pin: upstream minors change defaults (addons, access config)

  name               = var.name
  kubernetes_version = var.kubernetes_version
  vpc_id             = var.vpc_id
  subnet_ids         = var.subnet_ids

  # Nodes use the private API; optional administrator CIDRs enable laptop access.
  endpoint_public_access       = length(var.endpoint_public_access_cidrs) > 0
  endpoint_public_access_cidrs = var.endpoint_public_access_cidrs
  endpoint_private_access      = true

  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = true

  addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni = {
      before_compute = true
      # Without this the VPC CNI accepts NetworkPolicy objects but does NOT enforce them:
      # isolation would silently not exist. Must be on before any test namespace is created.
      configuration_values = jsonencode({ enableNetworkPolicy = tostring(var.enable_network_policy) })
    }
    eks-pod-identity-agent = { before_compute = true }
  }

  access_entries = var.ci_role_arn == null ? {} : {
    ci = {
      principal_arn     = var.ci_role_arn
      kubernetes_groups = [var.ci_kubernetes_group]
    }
  }

  eks_managed_node_groups = merge({
    # Small on-demand group for CoreDNS, the reaper CronJob and admission: must not be reclaimed.
    system = {
      instance_types = var.system_instance_types
      capacity_type  = "ON_DEMAND"
      min_size       = var.system_node_count
      max_size       = var.system_node_count
      desired_size   = var.system_node_count
      labels         = { role = "system" }
    }
    }, var.loadtest_node_group_enabled ? {

    # Tainted Spot pool; a zero desired size requires a separately installed node autoscaler.
    loadtest = {
      instance_types = var.loadtest_instance_types
      capacity_type  = "SPOT"
      min_size       = 0
      max_size       = var.loadtest_max_nodes
      desired_size   = var.loadtest_desired_nodes
      labels         = { role = "loadtest" }
      taints = {
        loadtest = {
          key    = "dedicated"
          value  = "loadtest"
          effect = "NO_SCHEDULE"
        }
      }
    }
  } : {})

  tags = var.tags
}
