data "archive_file" "dispatcher" {
  type        = "zip"
  source_dir  = "${path.module}/../.build/dispatcher"
  output_path = "${path.module}/../dist/dispatcher.zip"
}

resource "aws_iam_role" "dispatcher" {
  name = "${var.project_name}-dispatcher-${var.environment}"

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

# The dispatcher reads only event metadata, so its role holds no S3
# permissions at all.
resource "aws_iam_role_policy" "dispatcher" {
  name = "${var.project_name}-dispatcher-${var.environment}"
  role = aws_iam_role.dispatcher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "JobsTable"
        Effect = "Allow"
        Action = ["dynamodb:Query", "dynamodb:UpdateItem"]
        Resource = [
          aws_dynamodb_table.jobs.arn,
          "${aws_dynamodb_table.jobs.arn}/index/*"
        ]
      },
      {
        Sid      = "EnqueueJobs"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.jobs.arn
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.dispatcher.arn}:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "dispatcher" {
  name              = "/aws/lambda/${var.project_name}-dispatcher-${var.environment}"
  retention_in_days = 14
}

resource "aws_lambda_function" "dispatcher" {
  function_name    = "${var.project_name}-dispatcher-${var.environment}"
  role             = aws_iam_role.dispatcher.arn
  runtime          = "python3.12"
  handler          = "dispatcher.handler.handler"
  filename         = data.archive_file.dispatcher.output_path
  source_code_hash = data.archive_file.dispatcher.output_base64sha256
  memory_size      = 256
  timeout          = 10

  layers = [aws_lambda_layer_version.powertools.arn]

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      PROJECT_NAME   = var.project_name
      ENVIRONMENT    = var.environment
      JOBS_TABLE     = aws_dynamodb_table.jobs.name
      JOBS_QUEUE_URL = aws_sqs_queue.jobs.url
      UPLOADS_BUCKET = aws_s3_bucket.uploads.id
    }
  }

  depends_on = [aws_cloudwatch_log_group.dispatcher]
}

resource "aws_lambda_permission" "dispatcher_s3" {
  statement_id  = "AllowExecutionFromS3Bucket"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatcher.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.uploads.arn
}

resource "aws_s3_bucket_notification" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.dispatcher.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "uploads/"
  }

  depends_on = [aws_lambda_permission.dispatcher_s3]
}
