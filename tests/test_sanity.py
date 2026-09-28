import lib
from lib.settings import load_settings


def test_version():
    assert lib.__version__ == "0.1.0"


def test_settings_defaults():
    settings = load_settings()

    assert settings.aws_region == "eu-central-1"
    assert settings.environment == "dev"
    assert settings.jobs_table == "jobs"


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("UPLOADS_BUCKET", "uploads-bucket-dev")

    settings = load_settings()

    assert settings.aws_region == "us-east-1"
    assert settings.uploads_bucket == "uploads-bucket-dev"
