resource "aws_cognito_user_pool" "main" {
  name = "${var.project_name}-${var.environment}"

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
    require_uppercase = true
  }

  # Demo stage: only admins create users, self sign-up is closed.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
}

resource "aws_cognito_user_pool_client" "web" {
  name                  = "${var.project_name}-web-${var.environment}"
  user_pool_id          = aws_cognito_user_pool.main.id
  generate_secret       = false
  explicit_auth_flows   = ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  access_token_validity = 60
  id_token_validity     = 60

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}
