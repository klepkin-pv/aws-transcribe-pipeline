# Shared runtime dependencies for the pipeline functions. A layer keeps the
# function zips small and lets every function share one vetted version.
data "archive_file" "powertools_layer" {
  type        = "zip"
  source_dir  = "${path.module}/../.build/layer"
  output_path = "${path.module}/../dist/powertools-layer.zip"
}

resource "aws_lambda_layer_version" "powertools" {
  filename            = data.archive_file.powertools_layer.output_path
  layer_name          = "${var.project_name}-powertools-${var.environment}"
  compatible_runtimes = ["python3.12"]
  source_code_hash    = data.archive_file.powertools_layer.output_base64sha256
  license_info        = "MIT-0"
}
