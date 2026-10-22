output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_arn" {
  description = "EKS cluster ARN."
  value       = module.eks.cluster_arn
}

output "cluster_endpoint" {
  description = "Private API endpoint."
  value       = module.eks.cluster_endpoint
}

output "loadtest_max_nodes" {
  description = "Cost ceiling actually applied to the load-generator node group."
  value       = var.loadtest_max_nodes
}
