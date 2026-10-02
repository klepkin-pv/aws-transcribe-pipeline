resource "aws_sqs_queue" "jobs_dlq" {
  name                      = "${var.project_name}-jobs-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.project_name}-jobs-${var.environment}"
  visibility_timeout_seconds = 90 # well above the worker timeout
  receive_wait_time_seconds  = 10 # long polling

  # Messages that fail 3 processing attempts land in the DLQ and stay
  # inspectable for 14 days instead of looping forever.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })
}
