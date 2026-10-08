variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "eu-central-1"
}

variable "project_name" {
  description = "Project name used as a prefix for resource names and tags."
  type        = string
  default     = "aws-transcribe-pipeline"
}

variable "environment" {
  description = "Deployment environment name (dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "transcribe_language" {
  description = "Language code for AWS Transcribe jobs."
  type        = string
  default     = "ru-RU"
}

variable "bedrock_model_id" {
  description = <<-EOT
    Bedrock model id for transcript scoring; empty means scoring is not
    configured yet and the function role gets no bedrock:InvokeModel grant.
    Must be a model that accepts Converse on-demand calls in var.aws_region —
    several Bedrock models are reachable only through an inference profile,
    which is a different id and a different IAM resource.
  EOT
  type        = string
  default     = ""
}

variable "api_reserved_concurrency" {
  description = <<-EOT
    Reserved concurrency for the API function. 0 disables the reservation.
    New AWS accounts ship with a Lambda account limit of 10 concurrent
    executions and AWS refuses any reservation that would leave fewer than 10
    unreserved, so on a fresh account this must stay 0 until the quota
    (L-B99A9384) is raised. Keep it set on any account that runs more than one
    workload: it is what stops an API burst from starving the pipeline.
  EOT
  type        = number
  default     = 10
}

variable "worker_reserved_concurrency" {
  description = <<-EOT
    Reserved concurrency for the queue worker. 0 disables the reservation —
    see var.api_reserved_concurrency for the fresh-account quota caveat.
  EOT
  type        = number
  default     = 20
}

variable "alert_email" {
  description = "Email for operational alerts; empty disables the subscription."
  type        = string
  default     = ""
}
