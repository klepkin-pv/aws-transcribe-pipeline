resource "aws_dynamodb_table" "jobs" {
  name         = "${var.project_name}-jobs-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "gsi1pk"
    type = "S"
  }

  attribute {
    name = "gsi1sk"
    type = "S"
  }

  # Alternate access pattern: pipeline workers know only the job id,
  # so items are reachable by id alone through this GSI.
  global_secondary_index {
    name            = "gsi1-job-id"
    hash_key        = "gsi1pk"
    range_key       = "gsi1sk"
    projection_type = "ALL"
  }

  # Stale jobs are cleaned up automatically; see expires_at in src/lib/storage.py.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}
