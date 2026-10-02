"""Central configuration resolved from environment variables.

Lambda functions receive their configuration from Terraform-defined
environment variables; the same settings work locally against moto
because every value has a default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    project_name: str
    environment: str
    aws_region: str
    jobs_table: str
    uploads_bucket: str
    jobs_queue_url: str


def load_settings() -> Settings:
    return Settings(
        project_name=_env("PROJECT_NAME", "aws-transcribe-pipeline"),
        environment=_env("ENVIRONMENT", "dev"),
        aws_region=_env("AWS_REGION", "eu-central-1"),
        jobs_table=_env("JOBS_TABLE", "jobs"),
        uploads_bucket=_env("UPLOADS_BUCKET", ""),
        jobs_queue_url=_env("JOBS_QUEUE_URL", ""),
    )
