variable "name" {
  description = "EKS cluster name."
  type        = string
}

variable "kubernetes_version" {
  description = "EKS Kubernetes minor version, e.g. \"1.35\"."
  type        = string
  default     = "1.35"

  validation {
    condition     = can(regex("^1\\.[0-9]{2}$", var.kubernetes_version))
    error_message = "kubernetes_version must look like \"1.35\"."
  }
}

variable "vpc_id" {
  description = "Existing shared VPC (networking is owned by the platform network stack, not here)."
  type        = string
}

variable "subnet_ids" {
  description = "Existing subnets in at least two AZs. Public demo subnets must auto-assign public IPv4 addresses."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "subnet_ids needs at least two subnets (EKS requires two AZs)."
  }
}

variable "endpoint_public_access_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint (e.g. the CI runners' egress). Empty = private endpoint only."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for c in var.endpoint_public_access_cidrs : can(cidrhost(c, 0)) && c != "0.0.0.0/0"
    ])
    error_message = "endpoint_public_access_cidrs must be valid CIDRs and must not include 0.0.0.0/0."
  }
}

variable "loadtest_instance_types" {
  description = "Spot instance types for load-generator nodes. Several similar sizes so one spot pool running dry does not block tests."
  type        = list(string)
  default     = ["c6i.xlarge", "c5.xlarge", "c6a.xlarge", "m6i.xlarge"]

  validation {
    condition     = length(var.loadtest_instance_types) >= 2
    error_message = "Give at least two instance types to diversify spot capacity pools."
  }
}

variable "loadtest_max_nodes" {
  description = "Hard ceiling on load-generator nodes: the cluster-wide cost cap for all concurrent tests (max_nodes x spot price per hour)."
  type        = number
  default     = 4

  validation {
    condition     = var.loadtest_max_nodes >= 1 && var.loadtest_max_nodes <= 10 && floor(var.loadtest_max_nodes) == var.loadtest_max_nodes
    error_message = "loadtest_max_nodes must be a whole number from 1 to 10. Raising it past 10 is a budget decision, not a variable change."
  }
}

variable "ci_role_arn" {
  description = "CI IAM role given a cluster access entry. Mapped to a Kubernetes group only; no AWS-managed access policy."
  type        = string
  default     = null
}

variable "ci_kubernetes_group" {
  description = "Kubernetes group the CI role is mapped to; in-cluster RBAC binds this group to the load-test runner ClusterRole."
  type        = string
  default     = "loadtest-runners"
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "enable_network_policy" {
  description = "Turn on NetworkPolicy enforcement in the VPC CNI. Off means NetworkPolicy objects are accepted but ignored."
  type        = bool
  default     = true
}

variable "system_instance_types" {
  description = "Instance types for the always-on node group (CoreDNS, reaper, admission; also k6 when the loadtest group is disabled)."
  type        = list(string)
  default     = ["t3.medium"]
}

variable "system_node_count" {
  description = "Fixed size of the always-on node group."
  type        = number
  default     = 2
}

variable "loadtest_node_group_enabled" {
  description = "Create the tainted spot node group for k6. Disable for a small non-production deployment where k6 shares the system nodes (no node autoscaler is installed, so a scale-from-zero group would never get nodes)."
  type        = bool
  default     = true
}

variable "loadtest_desired_nodes" {
  description = "Initial Spot capacity. Set to zero only when a node autoscaler is installed."
  type        = number
  default     = 0
  validation {
    condition     = var.loadtest_desired_nodes >= 0 && var.loadtest_desired_nodes <= var.loadtest_max_nodes && floor(var.loadtest_desired_nodes) == var.loadtest_desired_nodes
    error_message = "Initial capacity must be an integer between zero and the pool maximum."
  }
}
