data "archive_file" "finalizer" {
  type        = "zip"
  source_dir  = "${path.module}/../.build/finalizer"
  output_path = "${path.module}/../dist/finalizer.zip"
}

# Undeliverable pipeline events park here after bounded retries.
resource "aws_sqs_queue" "events_dlq" {
  name                      = "${var.project_name}-events-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_iam_role" "finalizer" {
  name = "${var.project_name}-finalizer-${var.environment}"

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

resource "aws_iam_role_policy" "finalizer" {
  name = "${var.project_name}-finalizer-${var.environment}"
  role = aws_iam_role.finalizer.id

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
        Sid      = "ReadTranscripts"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.uploads.arn}/transcripts/*"
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.finalizer.arn}:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "finalizer" {
  name              = "/aws/lambda/${var.project_name}-finalizer-${var.environment}"
  retention_in_days = 14
}

resource "aws_lambda_function" "finalizer" {
  function_name    = "${var.project_name}-finalizer-${var.environment}"
  role             = aws_iam_role.finalizer.arn
  runtime          = "python3.12"
  handler          = "finalizer.handler.handler"
  filename         = data.archive_file.finalizer.output_path
  source_code_hash = data.archive_file.finalizer.output_base64sha256
  memory_size      = 256
  timeout          = 30

  environment {
    variables = {
      PROJECT_NAME        = var.project_name
      ENVIRONMENT         = var.environment
      JOBS_TABLE          = aws_dynamodb_table.jobs.name
      TRANSCRIBE_LANGUAGE = var.transcribe_language
      UPLOADS_BUCKET      = aws_s3_bucket.uploads.id
      BEDROCK_MODEL_ID    = var.bedrock_model_id
    }
  }

  depends_on = [aws_cloudwatch_log_group.finalizer]
}

resource "aws_cloudwatch_event_rule" "transcribe" {
  name = "${var.project_name}-transcribe-${var.environment}"

  event_pattern = jsonencode({
    source      = ["aws.transcribe"]
    detail-type = ["Transcribe Job State Change"]
    detail = {
      TranscriptionJobStatus = ["COMPLETED", "FAILED"]
    }
  })
}

resource "aws_lambda_permission" "finalizer_events" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.finalizer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.transcribe.arn
}

resource "aws_cloudwatch_event_target" "finalizer" {
  rule      = aws_cloudwatch_event_rule.transcribe.name
  target_id = "finalizer"
  arn       = aws_lambda_function.finalizer.arn

  # Bounded retries, then the event parks in the DLQ for inspection.
  retry_policy {
    maximum_retry_attempts       = 3
    maximum_event_age_in_seconds = 3600
  }

  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}
