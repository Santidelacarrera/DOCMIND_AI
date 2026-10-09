from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    environment: str = "development"
    database_url: str | None = None
    redis_url: str | None = None
    local_storage_path: Path = Path("./uploads")
    storage_provider: str = "local"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_kms_key_id: str | None = None
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None
    max_upload_bytes: int = 26_214_400
    max_pdf_pages: int = 100
    max_pdf_text_chars: int = 500_000
    max_ocr_pages: int = 25
    ocr_timeout_seconds: int = 60
    processing_timeout_seconds: int = 300
    stuck_job_seconds: int = 900
    # A QUEUED job older than this has most likely lost its broker message (Redis flush,
    # crash between commit and enqueue); the recovery task re-enqueues it.
    queued_stuck_seconds: int = 300
    # How many times recovery may re-enqueue the same job before failing it for good.
    max_job_recoveries: int = 3
    llm_provider: str = "mock"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: int = 30
    openai_max_retries: int = 2
    openai_max_output_tokens: int = 2_000
    openai_max_input_chars: int = 100_000
    openai_max_concurrency: int = 4
    cors_origins: str = "http://localhost:3000"
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60
    cookie_secure: bool | None = None
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_enabled: bool = True
    antivirus_provider: str = "disabled"
    clamav_host: str = "localhost"
    clamav_port: int = 3310
    confidence_review_threshold: float = 0.70
    # Mean OCR word confidence (0-1) below which a scanned document is sent to human review.
    ocr_review_threshold: float = 0.80
    # What to do with an LLM answer that violates the tenant's JSON Schema:
    #   review -- keep it but force human review (default; nothing is silently dropped)
    #   reject -- fail the job with LLM_SCHEMA_INVALID and store no extraction
    # A structurally broken answer (not the {"fields","confidence"} envelope) is always rejected.
    schema_violation_policy: Literal["review", "reject"] = "review"
    # Extra LLM calls allowed when an answer is rejected as invalid (0 = fail immediately).
    llm_invalid_output_retries: int = 1
    # Approximate USD price per 1M tokens, used for the cost estimate stored on each run.
    # Pricing changes: set these from your provider's current price list.
    openai_input_price_per_1m: float = 0.15
    openai_output_price_per_1m: float = 0.60

    # ---- Retention ---------------------------------------------------------------
    # Soft-deleted documents (and every derived record) are hard-deleted after this
    # many days. 0 = purge on the next retention run.
    retention_deleted_days: int = 30
    # Automatically expire live documents this many days after upload. 0 = keep until
    # a user deletes them (the default; retention is an operator decision).
    retention_document_days: int = 0
    free_pages_per_month: int = 20
    rate_limit_login: int = 10
    rate_limit_register: int = 5
    rate_limit_upload: int = 20
    rate_limit_api: int = 120

    # ---- Email (workspace invitations) --------------------------------------
    # Empty SMTP_HOST selects the console/log provider (development only).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True
    smtp_from: str = "DocMind AI <no-reply@docmind.ai>"
    invitation_expiry_hours: int = 72
    frontend_base_url: str = "http://localhost:3000"

    # ---- Observability --------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    otel_service_name: str = "docmind-api"
    # Unset disables tracing entirely (default for local dev/tests).
    otel_exporter_otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def validate_runtime_configuration(self) -> "Settings":
        production = self.environment.lower() in {"staging", "production"}
        if production:
            required = {
                "DATABASE_URL": self.database_url,
                "REDIS_URL": self.redis_url,
                "JWT_SECRET": self.jwt_secret,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError("Missing required production settings: " + ", ".join(missing))
            if len(self.jwt_secret or "") < 32:
                raise ValueError("JWT_SECRET must be at least 32 characters in staging/production")
            if self.cookie_samesite == "none" and self.cookie_secure is not True:
                raise ValueError("COOKIE_SAMESITE=none requires COOKIE_SECURE=true")
            if self.cookie_secure is not True:
                raise ValueError("COOKIE_SECURE=true is required in staging/production")
            origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
            if not origins or any(not origin.startswith("https://") for origin in origins):
                raise ValueError("CORS_ORIGINS must contain explicit HTTPS origins in staging/production")
            if self.llm_provider == "openai" and not self.openai_api_key:
                raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
            if self.antivirus_provider == "disabled":
                raise ValueError("ANTIVIRUS_PROVIDER must be configured in staging/production")
            if self.storage_provider == "local":
                raise ValueError("STORAGE_PROVIDER must use private object storage in staging/production")
            if not self.smtp_host:
                raise ValueError("SMTP_HOST is required in staging/production (invitation emails)")
            if not self.frontend_base_url.startswith("https://"):
                raise ValueError("FRONTEND_BASE_URL must be an explicit HTTPS origin in staging/production")
            if self.storage_provider == "s3":
                s3_required = {"S3_BUCKET": self.s3_bucket, "S3_REGION": self.s3_region}
                missing_s3 = [name for name, value in s3_required.items() if not value]
                if missing_s3:
                    raise ValueError("Missing required S3 settings: " + ", ".join(missing_s3))
                if not self.aws_access_key_id or not self.aws_secret_access_key:
                    raise ValueError(
                        "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required for S3 storage in staging/production"
                    )
        return self

    @property
    def effective_database_url(self) -> str:
        return self.database_url or "postgresql+psycopg://docmind:docmind@localhost:5432/docmind"

    @property
    def effective_redis_url(self) -> str:
        return self.redis_url or "redis://localhost:6379/0"

    @property
    def effective_jwt_secret(self) -> str:
        # Local development is deliberately explicit and is never accepted in staging/production.
        return self.jwt_secret or "local-development-only-not-for-deployment"

    @property
    def effective_cookie_secure(self) -> bool:
        return self.cookie_secure if self.cookie_secure is not None else self.environment.lower() != "development"


@lru_cache
def settings() -> Settings:
    return Settings()

