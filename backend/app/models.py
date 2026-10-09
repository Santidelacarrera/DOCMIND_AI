import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware_utc(value: datetime) -> datetime:
    """SQLite (used in tests) doesn't preserve tzinfo through a round trip even
    for a ``DateTime(timezone=True)`` column, so a value just read back from it
    can come back naive while everything we compare it against is UTC-aware.
    Postgres (staging/production) always returns it aware already, so this is
    a no-op there."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


UUID = Uuid


class Role(str, enum.Enum):
    owner = "OWNER"
    admin = "ADMIN"
    member = "MEMBER"
    viewer = "VIEWER"


class Plan(str, enum.Enum):
    free = "FREE"
    pro = "PRO"
    enterprise = "ENTERPRISE"


class JobStatus(str, enum.Enum):
    uploaded = "UPLOADED"
    queued = "QUEUED"
    processing = "PROCESSING"
    ocr_processing = "OCR_PROCESSING"
    extracting = "EXTRACTING"
    validating = "VALIDATING"
    completed = "COMPLETED"
    failed = "FAILED"
    cancelled = "CANCELLED"


class RunStatus(str, enum.Enum):
    processing = "PROCESSING"
    completed = "COMPLETED"
    failed = "FAILED"


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    plan: Mapped[Plan] = mapped_column(Enum(Plan), default=Plan.free)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_organization_member"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.member)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_document_version"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number", name="uq_document_page"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False)
    ocr_confidence: Mapped[float | None]


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (UniqueConstraint("document_id", "attempt", name="uq_job_attempt"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.queued, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    # Schema version the user explicitly picked at upload/reprocess time. When unset,
    # the worker falls back to the organization/project's active schema (see
    # app.worker.active_schema_version).
    requested_schema_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_schema_versions.id"), index=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessingStep(Base):
    __tablename__ = "processing_steps"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.processing)
    schema_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_schema_versions.id")
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None]
    # Business-rule / JSON-schema validation findings computed right after extraction,
    # before the result is considered final. Each item looks like:
    # {"field": "total_amount", "rule": "schema" | "low_confidence", "message": "..."}
    validation_issues: Mapped[list[Any]] = mapped_column(JSON, default=list)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtractionSchema(Base):
    __tablename__ = "extraction_schemas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtractionSchemaVersion(Base):
    __tablename__ = "extraction_schema_versions"
    __table_args__ = (UniqueConstraint("schema_id", "version", name="uq_schema_version"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    schema_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_schemas.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    json_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    prompt_instructions: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtractionField(Base):
    __tablename__ = "extraction_fields"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(256))
    original_value: Mapped[Any] = mapped_column(JSON)
    value: Mapped[Any] = mapped_column(JSON)
    confidence: Mapped[float | None]
    page_number: Mapped[int | None]
    source_text: Mapped[str | None] = mapped_column(Text)
    bounding_box: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    manually_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scopes: Mapped[dict[str, Any]] = mapped_column(JSON, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    metric: Mapped[str] = mapped_column(String(48), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class InvitationStatus(str, enum.Enum):
    pending = "PENDING"
    accepted = "ACCEPTED"
    revoked = "REVOKED"


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.member)
    # Only the salted hash is stored; the raw token is emailed once and never persisted.
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[InvitationStatus] = mapped_column(
        Enum(InvitationStatus), default=InvitationStatus.pending, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Webhook(Base):
    __tablename__ = "webhooks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    description: Mapped[str | None] = mapped_column(String(160))
    # The signing secret itself is never stored: app.webhooks.signing_secret() derives
    # it deterministically from JWT_SECRET + the webhook id, the same way on every
    # delivery, and is shown to the caller once at creation time.
    events: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["document.completed"])
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivery_status: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhooks.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(80))
    status_code: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
