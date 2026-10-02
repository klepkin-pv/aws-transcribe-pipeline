data "archive_file" "worker" {
  type        = "zip"
  source_dir  = "${path.module}/../.build/worker"
  output_path = "${path.module}/../dist/worker.zip"
}

resource "aws_iam_role" "worker" {
  name = "${var.project_name}-worker-${var.environment}"

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

resource "aws_iam_role_policy" "worker" {
  name = "${var.project_name}-worker-${var.environment}"
  role = aws_iam_role.worker.id

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
        Sid      = "ConsumeJobsQueue"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.jobs.arn
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.worker.arn}:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${var.project_name}-worker-${var.environment}"
  retention_in_days = 14
}

resource "aws_lambda_function" "worker" {
  function_name    = "${var.project_name}-worker-${var.environment}"
  role             = aws_iam_role.worker.arn
  runtime          = "python3.12"
  handler          = "worker.handler.handler"
  filename         = data.archive_file.worker.output_path
  source_code_hash = data.archive_file.worker.output_base64sha256
  memory_size      = 256
  timeout          = 30

  environment {
    variables = {
      PROJECT_NAME = var.project_name
      ENVIRONMENT  = var.environment
      JOBS_TABLE   = aws_dynamodb_table.jobs.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.worker]
}

# SQS scales the worker automatically with queue depth; ReportBatchItemFailures
# keeps the rest of a batch alive when a single message fails.
resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn        = aws_sqs_queue.jobs.arn
  function_name           = aws_lambda_function.worker.arn
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
}
