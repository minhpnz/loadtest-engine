variable "bucket_name" {
  description = "Globally unique bucket name for k6 summaries / raw results."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be a valid S3 bucket name (3-63 chars, lowercase, digits, '.', '-')."
  }
}

variable "retention_days" {
  description = "Days before result objects expire. Results are evidence for a release decision, not an archive; expiry bounds storage cost."
  type        = number
  default     = 30

  validation {
    condition     = var.retention_days > 0 && floor(var.retention_days) == var.retention_days
    error_message = "retention_days must be a whole number > 0 (0 would mean results are never expired or deleted immediately)."
  }
}

variable "force_destroy" {
  description = "Allow destroying a non-empty bucket. Keep false outside throwaway environments."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
