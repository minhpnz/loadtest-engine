output "cluster_name" {
  value = module.cluster.cluster_name
}
output "github_role_arn" {
  value = module.github_role.role_arn
}
output "results_bucket" {
  value = module.results.bucket_name
}
output "runtime_limits" {
  value = local.limits
}
