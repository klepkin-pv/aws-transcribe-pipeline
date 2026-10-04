resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts-${var.environment}"
}

# Confirmed by clicking the link in the confirmation email.
resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarm_actions = [aws_sns_topic.alerts.arn]

  lambda_alarms = {
    api        = aws_lambda_function.api.function_name
    dispatcher = aws_lambda_function.dispatcher.function_name
    worker     = aws_lambda_function.worker.function_name
    finalizer  = aws_lambda_function.finalizer.function_name
  }

  dlq_alarms = {
    jobs_dlq   = aws_sqs_queue.jobs_dlq.name
    events_dlq = aws_sqs_queue.events_dlq.name
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = local.lambda_alarms
  alarm_name          = "${each.value}-errors"
  alarm_description   = "Lambda errors >= 5 in 5 minutes"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = local.alarm_actions
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  for_each            = local.lambda_alarms
  alarm_name          = "${each.value}-throttles"
  alarm_description   = "Lambda throttles detected"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = local.alarm_actions
  treat_missing_data  = "notBreaching"
}

# A non-empty DLQ always means manual attention: a job or an event exhausted
# its retries.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  for_each            = local.dlq_alarms
  alarm_name          = "${each.value}-not-empty"
  alarm_description   = "Dead letter queue is not empty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = each.value }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = local.alarm_actions
  treat_missing_data  = "notBreaching"
}

# Old messages mean the queue drains slower than it fills: the worker is
# either throttled or failing.
resource "aws_cloudwatch_metric_alarm" "jobs_queue_backlog_age" {
  alarm_name          = "${var.project_name}-jobs-queue-backlog-age-${var.environment}"
  alarm_description   = "Oldest message in the jobs queue is older than 10 minutes"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.jobs.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 600
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = local.alarm_actions
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.project_name}-api-5xx-${var.environment}"
  alarm_description   = "API Gateway 5xx responses >= 5 in 5 minutes"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.http.id }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = local.alarm_actions
  treat_missing_data  = "notBreaching"
}
