resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.project_name}-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Pipeline jobs (EMF)"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["TranscribePipeline", "JobEnqueued", "service", "dispatcher"],
            ["TranscribePipeline", "JobClaimed", "service", "worker"],
            ["TranscribePipeline", "JobDone", "service", "finalizer"],
            ["TranscribePipeline", "JobFailed", "service", "finalizer"]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Jobs queue"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Maximum"
          period = 300
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.jobs.name],
            ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", aws_sqs_queue.jobs.name, { stat = "Maximum", y = 20 }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Dead letter queues (must stay empty)"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Maximum"
          period = 300
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.jobs_dlq.name],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.events_dlq.name]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Lambda errors"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.api.function_name],
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.dispatcher.function_name],
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.worker.function_name],
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.finalizer.function_name]
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "API duration p99 per function"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "p99"
          period = 300
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.api.function_name],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.dispatcher.function_name],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.worker.function_name],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.finalizer.function_name]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "API Gateway 5xx"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/ApiGateway", "5xx", "ApiId", aws_apigatewayv2_api.http.id]
          ]
        }
      }
    ]
  })
}

# Saved Logs Insights queries for the runbook: triage starts here.
resource "aws_cloudwatch_query_definition" "failed_jobs" {
  name = "pipeline/failed-jobs"

  log_group_names = [
    aws_cloudwatch_log_group.worker.name,
    aws_cloudwatch_log_group.finalizer.name,
  ]

  query_string = <<-EOT
    fields @timestamp, @message
    | filter @message like /failed/
    | sort @timestamp desc
    | limit 100
  EOT
}

resource "aws_cloudwatch_query_definition" "skipped_messages" {
  name = "pipeline/skipped-messages"

  log_group_names = [
    aws_cloudwatch_log_group.dispatcher.name,
    aws_cloudwatch_log_group.worker.name,
    aws_cloudwatch_log_group.finalizer.name,
  ]

  query_string = <<-EOT
    fields @timestamp, @message
    | filter @message like /Skipping/
    | stats count() by @message
    | sort count desc
    | limit 50
  EOT
}
