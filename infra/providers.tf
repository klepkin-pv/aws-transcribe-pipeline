# Local state for now; switches to an S3 backend with DynamoDB locking
# once the deployment AWS account is provisioned.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
