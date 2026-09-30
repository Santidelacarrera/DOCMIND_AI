import pytest
from pydantic import ValidationError

from app.core import Settings


def staging_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "staging",
        "database_url": "postgresql+psycopg://user:password@db.example.invalid:5432/docmind",
        "redis_url": "redis://:password@redis.example.invalid:6379/0",
        "jwt_secret": "a" * 32,
        "cookie_secure": True,
        "cors_origins": "https://staging.example.invalid",
        "storage_provider": "s3",
        "s3_bucket": "private-staging-bucket",
        "s3_region": "example-region-1",
        "s3_kms_key_id": "alias/docmind-staging",
        "aws_access_key_id": "test-access-key",
        "aws_secret_access_key": "test-secret-key",
        "antivirus_provider": "clamav",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_staging_requires_core_secrets() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        staging_settings(jwt_secret=None)


def test_staging_requires_private_storage_and_https_cors() -> None:
    with pytest.raises(ValidationError, match="STORAGE_PROVIDER"):
        staging_settings(storage_provider="local")
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        staging_settings(cors_origins="http://localhost:3000")


def test_openai_secret_is_conditional() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        staging_settings(llm_provider="openai", openai_api_key=None)


def test_staging_s3_requires_runtime_credentials() -> None:
    with pytest.raises(ValidationError, match="AWS_ACCESS_KEY_ID"):
        staging_settings(aws_access_key_id=None)
