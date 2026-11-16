"""Configurable SaaS commercial plans, monthly usage metering, quota enforcement and alerts."""

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import settings
from app.models import (
    Document,
    Invitation,
    InvitationStatus,
    Organization,
    OrganizationMember,
    OrganizationSubscription,
    Plan,
    UsageRecord,
)

logger = logging.getLogger("docmind.plans")

PLAN_ORDER = ("FREE", "STARTER", "PROFESSIONAL", "BUSINESS", "ENTERPRISE")
LEGACY_PLAN_ALIASES = {"PRO": "PROFESSIONAL"}


@dataclass(frozen=True)
class PlanSpec:
    code: str
    display_name: str
    description: str
    monthly_price_usd: float | None  # configurable reference; None = custom contract
    annual_price_usd: float | None
    max_users: int | None
    max_documents: int | None
    max_storage_bytes: int | None
    max_upload_bytes: int
    max_pages_per_month: int | None
    max_queries_per_month: int | None
    max_ai_tokens_per_month: int | None
    features: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["features"] = list(self.features)
        return data


DEFAULT_PLANS: dict[str, PlanSpec] = {
    "FREE": PlanSpec(
        code="FREE",
        display_name="Free / Trial",
        description="Controlled evaluation for small teams with local document extraction and search.",
        monthly_price_usd=0.0,
        annual_price_usd=0.0,
        max_users=5,
        max_documents=50,
        max_storage_bytes=100 * 1024 * 1024,
        max_upload_bytes=10 * 1024 * 1024,
        max_pages_per_month=20,
        max_queries_per_month=200,
        max_ai_tokens_per_month=100_000,
        features=("pdf", "docx", "txt", "csv", "xlsx", "pptx", "image_ocr", "extractive_ask", "exports"),
    ),
    "STARTER": PlanSpec(
        code="STARTER",
        display_name="Starter",
        description="Small teams automating multi-format document ingestion, entities and API keys.",
        monthly_price_usd=49.0,
        annual_price_usd=470.0,
        max_users=10,
        max_documents=500,
        max_storage_bytes=1024 * 1024 * 1024,
        max_upload_bytes=26_214_400,
        max_pages_per_month=500,
        max_queries_per_month=1_000,
        max_ai_tokens_per_month=1_000_000,
        features=(
            "pdf", "docx", "txt", "csv", "xlsx", "pptx", "image_ocr",
            "extractive_ask", "exports", "api_keys", "auto_classification", "entity_extraction",
        ),
    ),
    "PROFESSIONAL": PlanSpec(
        code="PROFESSIONAL",
        display_name="Professional",
        description="Growing organizations with verified generative RAG, comparisons, webhooks and custom schemas.",
        monthly_price_usd=149.0,
        annual_price_usd=1_430.0,
        max_users=30,
        max_documents=5_000,
        max_storage_bytes=10 * 1024 * 1024 * 1024,
        max_upload_bytes=52_428_800,
        max_pages_per_month=5_000,
        max_queries_per_month=10_000,
        max_ai_tokens_per_month=10_000_000,
        features=(
            "pdf", "docx", "txt", "csv", "xlsx", "pptx", "image_ocr",
            "extractive_ask", "generative_ask", "exports", "api_keys",
            "auto_classification", "entity_extraction", "document_comparison",
            "summarization", "webhooks", "custom_schemas",
        ),
    ),
    "BUSINESS": PlanSpec(
        code="BUSINESS",
        display_name="Business",
        description="High-volume departments with cross-encoder reranking, priority processing and audit exports.",
        monthly_price_usd=399.0,
        annual_price_usd=3_830.0,
        max_users=100,
        max_documents=25_000,
        max_storage_bytes=50 * 1024 * 1024 * 1024,
        max_upload_bytes=104_857_600,
        max_pages_per_month=25_000,
        max_queries_per_month=50_000,
        max_ai_tokens_per_month=50_000_000,
        features=(
            "pdf", "docx", "txt", "csv", "xlsx", "pptx", "image_ocr",
            "extractive_ask", "generative_ask", "reranking", "exports", "api_keys",
            "auto_classification", "entity_extraction", "document_comparison",
            "summarization", "webhooks", "custom_schemas", "audit_export",
        ),
    ),
    "ENTERPRISE": PlanSpec(
        code="ENTERPRISE",
        display_name="Enterprise",
        description="Dedicated isolation, PostgreSQL RLS, KMS encryption and custom contractual limits.",
        monthly_price_usd=None,
        annual_price_usd=None,
        max_users=None,
        max_documents=None,
        max_storage_bytes=None,
        max_upload_bytes=104_857_600,
        max_pages_per_month=None,
        max_queries_per_month=None,
        max_ai_tokens_per_month=None,
        features=(
            "pdf", "docx", "txt", "csv", "xlsx", "pptx", "image_ocr",
            "extractive_ask", "generative_ask", "reranking", "exports", "api_keys",
            "auto_classification", "entity_extraction", "document_comparison",
            "summarization", "webhooks", "custom_schemas", "audit_export",
            "dedicated_rls", "kms_encryption", "sso_ready",
        ),
    ),
}


def canonical_plan(code: str | Plan) -> str:
    raw = code.value if isinstance(code, Plan) else str(code).strip().upper()
    return LEGACY_PLAN_ALIASES.get(raw, raw)


def db_plan_enum(code: str) -> Plan:
    norm = canonical_plan(code)
    if norm == "PROFESSIONAL":
        try:
            return Plan("PROFESSIONAL")
        except ValueError:
            return Plan.pro
    return Plan(norm)


def _load_overrides() -> dict[str, dict[str, Any]]:
    raw = getattr(settings(), "plans_config_json", None)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        logger.warning("invalid PLANS_CONFIG_JSON; using built-in plan defaults")
        return {}


def plan_spec(code: str | Plan, custom_limits: dict[str, Any] | None = None) -> PlanSpec:
    norm = canonical_plan(code)
    base = DEFAULT_PLANS.get(norm, DEFAULT_PLANS["FREE"])
    data = base.to_dict()
    if norm == "FREE":
        data["max_pages_per_month"] = settings().free_pages_per_month
    overrides = _load_overrides().get(norm)
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key in data and key != "code":
                data[key] = tuple(value) if key == "features" and isinstance(value, list) else value
    if isinstance(custom_limits, dict):
        for key, value in custom_limits.items():
            if key in data and key != "code":
                data[key] = tuple(value) if key == "features" and isinstance(value, list) else value
    if isinstance(data["features"], list):
        data["features"] = tuple(str(x) for x in data["features"])
    return PlanSpec(**data)


def all_plans() -> list[dict[str, Any]]:
    return [plan_spec(code).to_dict() for code in PLAN_ORDER]


def billing_period(now: datetime | None = None) -> tuple[str, datetime, datetime]:
    current = now or datetime.now(UTC)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.strftime("%Y-%m"), start, end


def get_or_build_subscription(db: Session, org: Organization) -> OrganizationSubscription | None:
    return db.scalar(
        select(OrganizationSubscription).where(OrganizationSubscription.organization_id == org.id)
    )


def effective_plan(db: Session, org: Organization) -> tuple[PlanSpec, OrganizationSubscription | None]:
    sub = get_or_build_subscription(db, org)
    if sub is not None and sub.status in ("unpaid", "canceled", "expired"):
        return plan_spec("FREE"), sub
    code = sub.plan if sub is not None else org.plan
    custom = sub.custom_limits if sub is not None else None
    return plan_spec(code, custom_limits=custom), sub


def _period_metric_sum(db: Session, org_id: uuid.UUID, metric: str, period_start: datetime) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
                UsageRecord.organization_id == org_id,
                UsageRecord.metric == metric,
                UsageRecord.created_at >= period_start,
            )
        )
        or 0
    )


def usage_snapshot(db: Session, org: Organization, now: datetime | None = None) -> dict[str, Any]:
    period_label, period_start, period_end = billing_period(now)
    spec, sub = effective_plan(db, org)
    alert_threshold = sub.alert_threshold_pct if sub is not None else 80

    pages_reserved = _period_metric_sum(db, org.id, "pages_reserved", period_start)
    pages_processed = _period_metric_sum(db, org.id, "pages_processed", period_start)
    documents_processed = _period_metric_sum(db, org.id, "documents_processed", period_start)
    queries_executed = _period_metric_sum(db, org.id, "queries_executed", period_start)
    ai_tokens_used = _period_metric_sum(db, org.id, "ai_tokens_used", period_start)
    ai_cost_microusd = _period_metric_sum(db, org.id, "ai_cost_microusd", period_start)

    active_documents = int(
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.organization_id == org.id, Document.deleted_at.is_(None))
        )
        or 0
    )
    storage_bytes = int(
        db.scalar(
            select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
                Document.organization_id == org.id, Document.deleted_at.is_(None)
            )
        )
        or 0
    )
    member_count = int(
        db.scalar(
            select(func.count())
            .select_from(OrganizationMember)
            .where(OrganizationMember.organization_id == org.id)
        )
        or 0
    )
    pending_invites = int(
        db.scalar(
            select(func.count())
            .select_from(Invitation)
            .where(
                Invitation.organization_id == org.id,
                Invitation.status == InvitationStatus.pending,
            )
        )
        or 0
    )

    meters = [
        ("pages_per_month", pages_reserved, spec.max_pages_per_month),
        ("documents", active_documents, spec.max_documents),
        ("storage_bytes", storage_bytes, spec.max_storage_bytes),
        ("queries_per_month", queries_executed, spec.max_queries_per_month),
        ("ai_tokens_per_month", ai_tokens_used, spec.max_ai_tokens_per_month),
        ("users", member_count + pending_invites, spec.max_users),
    ]
    alerts: list[dict[str, Any]] = []
    consumption: dict[str, dict[str, Any]] = {}
    for name, used, limit in meters:
        pct = round((used / limit) * 100, 1) if limit and limit > 0 else 0.0
        state = "ok"
        if limit is not None and used >= limit:
            state = "exceeded"
        elif limit is not None and pct >= alert_threshold:
            state = "warning"
        consumption[name] = {"used": used, "limit": limit, "percent": pct if limit is not None else None, "status": state}
        if state in ("warning", "exceeded"):
            alerts.append({"resource": name, "status": state, "used": used, "limit": limit, "percent": pct})
    if sub is not None and sub.status in ("past_due", "unpaid", "canceled", "expired"):
        alerts.append(
            {
                "resource": "subscription_status",
                "status": "exceeded" if sub.status in ("unpaid", "expired") else "warning",
                "used": sub.failed_payment_count,
                "limit": 3,
                "percent": 100.0,
                "detail": f"subscription_{sub.status}",
            }
        )

    return {
        "period": period_label,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "plan": spec.to_dict(),
        "subscription_status": sub.status if sub is not None else "active",
        "billing_cycle": sub.billing_cycle if sub is not None else "monthly",
        "alert_threshold_pct": alert_threshold,
        "consumption": consumption,
        "alerts": alerts,
        "totals": {
            "pages_reserved": pages_reserved,
            "pages_processed": pages_processed,
            "documents_processed": documents_processed,
            "active_documents": active_documents,
            "storage_bytes": storage_bytes,
            "queries_executed": queries_executed,
            "ai_tokens_used": ai_tokens_used,
            "ai_cost_usd": round(ai_cost_microusd / 1_000_000, 6),
            "members": member_count,
            "pending_invitations": pending_invites,
        },
    }


def check_upload_quota(
    db: Session, org_id: uuid.UUID, *, file_bytes: int, page_count: int, is_new_document: bool = True
) -> None:
    """Enforce plan upload size, page, document count and storage quotas with row locking."""
    org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    spec, _ = effective_plan(db, org)
    if file_bytes > spec.max_upload_bytes:
        raise HTTPException(413, "FILE_TOO_LARGE")

    _, period_start, _ = billing_period()
    if spec.max_pages_per_month is not None:
        reserved_pages = _period_metric_sum(db, org_id, "pages_reserved", period_start)
        if reserved_pages + page_count > spec.max_pages_per_month:
            raise HTTPException(402, "QUOTA_EXCEEDED")

    if is_new_document and spec.max_documents is not None:
        active_docs = int(
            db.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.organization_id == org_id, Document.deleted_at.is_(None))
            )
            or 0
        )
        if active_docs + 1 > spec.max_documents:
            raise HTTPException(402, "QUOTA_EXCEEDED")

    if spec.max_storage_bytes is not None:
        used_bytes = int(
            db.scalar(
                select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
                    Document.organization_id == org_id, Document.deleted_at.is_(None)
                )
            )
            or 0
        )
        if used_bytes + file_bytes > spec.max_storage_bytes:
            raise HTTPException(402, "QUOTA_EXCEEDED")


def check_query_quota(db: Session, org_id: uuid.UUID) -> None:
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    spec, _ = effective_plan(db, org)
    _, period_start, _ = billing_period()
    if spec.max_queries_per_month is not None:
        used = _period_metric_sum(db, org_id, "queries_executed", period_start)
        if used + 1 > spec.max_queries_per_month:
            raise HTTPException(402, "QUOTA_EXCEEDED")
    if spec.max_ai_tokens_per_month is not None:
        tokens_used = _period_metric_sum(db, org_id, "ai_tokens_used", period_start)
        if tokens_used >= spec.max_ai_tokens_per_month:
            raise HTTPException(402, "QUOTA_EXCEEDED")


def check_user_seat_quota(db: Session, org_id: uuid.UUID) -> None:
    org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    spec, _ = effective_plan(db, org)
    if spec.max_users is None:
        return
    members = int(
        db.scalar(
            select(func.count())
            .select_from(OrganizationMember)
            .where(OrganizationMember.organization_id == org_id)
        )
        or 0
    )
    pending = int(
        db.scalar(
            select(func.count())
            .select_from(Invitation)
            .where(
                Invitation.organization_id == org_id,
                Invitation.status == InvitationStatus.pending,
            )
        )
        or 0
    )
    if members + pending + 1 > spec.max_users:
        raise HTTPException(402, "QUOTA_EXCEEDED")


def record_query_usage(db: Session, org_id: uuid.UUID, *, tokens: int = 0) -> None:
    db.add(UsageRecord(organization_id=org_id, metric="queries_executed", quantity=1))
    if tokens > 0:
        db.add(UsageRecord(organization_id=org_id, metric="ai_tokens_used", quantity=tokens))


def monthly_statements(db: Session, org: Organization, *, months: int = 6) -> list[dict[str, Any]]:
    """Aggregate UsageRecord rows into monthly account statements for billing/audit."""
    spec, sub = effective_plan(db, org)
    rows = db.execute(select(UsageRecord).where(UsageRecord.organization_id == org.id)).scalars().all()
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        period = row.created_at.strftime("%Y-%m")
        bucket = grouped.setdefault(
            period,
            {
                "pages_reserved": 0,
                "pages_processed": 0,
                "documents_processed": 0,
                "queries_executed": 0,
                "ai_tokens_used": 0,
                "ai_cost_microusd": 0,
            },
        )
        if row.metric in bucket:
            bucket[row.metric] += int(row.quantity or 0)
    current_period, _, _ = billing_period()
    grouped.setdefault(
        current_period,
        {
            "pages_reserved": 0,
            "pages_processed": 0,
            "documents_processed": 0,
            "queries_executed": 0,
            "ai_tokens_used": 0,
            "ai_cost_microusd": 0,
        },
    )
    statements = []
    base_price = (
        spec.annual_price_usd / 12.0
        if (sub and sub.billing_cycle == "annual" and spec.annual_price_usd is not None)
        else spec.monthly_price_usd
    )
    for period in sorted(grouped.keys(), reverse=True)[:months]:
        m = grouped[period]
        statements.append(
            {
                "period": period,
                "plan_code": spec.code,
                "plan_name": spec.display_name,
                "estimated_subscription_usd": round(base_price, 2) if base_price is not None else None,
                "estimated_ai_cost_usd": round(m["ai_cost_microusd"] / 1_000_000, 6),
                "metrics": m,
            }
        )
    return statements
