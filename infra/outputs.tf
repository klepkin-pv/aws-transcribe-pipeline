output "user_pool_id" {
  description = "Cognito user pool id; used by scripts to create demo users."
  value       = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  description = "App client id used to obtain JWT tokens."
  value       = aws_cognito_user_pool_client.web.id
}

output "api_endpoint" {
  description = "Base URL of the HTTP API."
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "jobs_table" {
  description = "DynamoDB table holding job state."
  value       = aws_dynamodb_table.jobs.name
}

output "uploads_bucket" {
  description = "S3 bucket receiving the uploads."
  value       = aws_s3_bucket.uploads.id
}

output "alerts_topic_arn" {
  description = "SNS topic for operational alarms."
  value       = aws_sns_topic.alerts.arn
}
