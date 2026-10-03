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
  description = "Bedrock model id for transcript scoring; empty means scoring is not configured yet."
  type        = string
  default     = ""
}
