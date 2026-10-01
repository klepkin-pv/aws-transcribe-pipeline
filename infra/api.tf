# --- Packaging --------------------------------------------------------------
# `make package-api` assembles .build/api (function code + Linux/py3.12 wheels)
# and archive_file zips it for deployment. `terraform validate` does not
# evaluate data sources, so CI works without a built package on disk.

data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../.build/api"
  output_path = "${path.module}/../dist/api.zip"
}

# --- IAM: least privilege, scoped to the exact table/bucket/prefix ----------

resource "aws_iam_role" "api" {
  name = "${var.project_name}-api-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "api" {
  name = "${var.project_name}-api-${var.environment}"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "JobsTable"
        Effect = "Allow"
        Action = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem"]
        Resource = [
          aws_dynamodb_table.jobs.arn,
          "${aws_dynamodb_table.jobs.arn}/index/*"
        ]
      },
      {
        Sid      = "PresignedUploads"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.uploads.arn}/uploads/*"
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.api.arn}:*"
      }
    ]
  })
}

# --- Lambda function ---------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${var.project_name}-api-${var.environment}"
  retention_in_days = 14
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.project_name}-api-${var.environment}"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "api.handler.handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  memory_size      = 256
  timeout          = 10

  environment {
    variables = {
      PROJECT_NAME   = var.project_name
      ENVIRONMENT    = var.environment
      JOBS_TABLE     = aws_dynamodb_table.jobs.name
      UPLOADS_BUCKET = aws_s3_bucket.uploads.id
    }
  }

  depends_on = [aws_cloudwatch_log_group.api]
}

# --- HTTP API ----------------------------------------------------------------

resource "aws_apigatewayv2_api" "http" {
  name          = "${var.project_name}-${var.environment}"
  protocol_type = "HTTP"
}

# JWT authorizer validates the Cognito access token; the API function then
# trusts the verified claims in requestContext (see src/api/main.py).
resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.http.id
  name             = "cognito-jwt"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web.id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

# $default route behind the JWT authorizer: any path is rejected without a
# valid token — secure by default as routes are added later.
resource "aws_apigatewayv2_route" "default" {
  api_id             = aws_apigatewayv2_api.http.id
  route_key          = "$default"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowExecutionFromHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*"
}
