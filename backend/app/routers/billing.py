"""SaaS commercial plans, payment gateway integration, webhooks, metering and statements."""

import hashlib
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import api_support, billing_providers, plans, rls
from app.api_support import audit
from app.core import settings
from app.db import get_db
from app.models import (
    BillingTransaction,
    BillingWebhookEvent,
    Organization,
    OrganizationSubscription,
    Role,
    User,
    utcnow,
)
from app.security import ApiPrincipal, current_user, membership, require_user

router = APIRouter()


@router.get("/api/v1/plans")
def public_plans() -> list[dict[str, Any]]:
    """Public configurable SaaS plan catalog."""
    return plans.all_plans()


@router.get("/api/v1/billing/providers")
def list_payment_providers(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Supported payment gateway adapters, Chile capabilities and active configuration."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="usage:read")
    cfg = settings()
    return {
        "configured_provider": cfg.billing_provider,
        "live_charges_enabled": bool(cfg.billing_live_enabled),
        "providers": billing_providers.provider_catalog(),
    }


@router.get("/api/v1/billing/plans")
def org_plans(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="usage:read")
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    active, sub = plans.effective_plan(db, org)
    return {
        "current_plan": active.code,
        "subscription_status": sub.status if sub is not None else "active",
        "plans": plans.all_plans(),
    }


def _subscription_dict(
    org: Organization, spec: plans.PlanSpec, sub: OrganizationSubscription | None
) -> dict[str, Any]:
    _, start, end = plans.billing_period()
    return {
        "organization_id": str(org.id),
        "plan": spec.to_dict(),
        "configured_plan": sub.plan if sub is not None else plans.canonical_plan(org.plan),
        "status": sub.status if sub is not None else "active",
        "billing_cycle": sub.billing_cycle if sub is not None else "monthly",
        "payment_provider": sub.payment_provider if sub is not None else settings().billing_provider,
        "external_customer_id": sub.external_customer_id if sub is not None else None,
        "external_subscription_id": sub.external_subscription_id if sub is not None else None,
        "alert_threshold_pct": sub.alert_threshold_pct if sub is not None else 80,
        "failed_payment_count": sub.failed_payment_count if sub is not None else 0,
        "cancel_at_period_end": bool(sub.cancel_at_period_end) if sub is not None else False,
        "current_period_start": (sub.current_period_start if sub is not None else start),
        "current_period_end": (
            sub.current_period_end if sub is not None and sub.current_period_end else end
        ),
    }


@router.get("/api/v1/billing/subscription")
def get_subscription(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin, scope="usage:read")
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    spec, sub = plans.effective_plan(db, org)
    return _subscription_dict(org, spec, sub)


class SubscriptionUpdateInput(BaseModel):
    plan: Literal["FREE", "STARTER", "PRO", "PROFESSIONAL", "BUSINESS", "ENTERPRISE"] | None = None
    billing_cycle: Literal["monthly", "annual"] | None = None
    alert_threshold_pct: int | None = Field(default=None, ge=50, le=99)


def _ensure_subscription(db: Session, org: Organization) -> OrganizationSubscription:
    sub = plans.get_or_build_subscription(db, org)
    _, start, end = plans.billing_period()
    if sub is None:
        sub = OrganizationSubscription(
            organization_id=org.id,
            plan=plans.canonical_plan(org.plan),
            status="active",
            billing_cycle="monthly",
            payment_provider=settings().billing_provider,
            current_period_start=start,
            current_period_end=end,
        )
        db.add(sub)
        db.flush()
    return sub


@router.patch("/api/v1/billing/subscription")
def update_subscription(
    organization_id: str,
    payload: SubscriptionUpdateInput,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update subscription configuration (sandbox/admin mode; no live payment charges executed)."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.owner)
    org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")

    sub = _ensure_subscription(db, org)
    if payload.plan is not None:
        canonical = plans.canonical_plan(payload.plan)
        sub.plan = canonical
        sub.status = "active"
        sub.failed_payment_count = 0
        sub.cancel_at_period_end = False
        org.plan = plans.db_plan_enum(canonical)
    if payload.billing_cycle is not None:
        sub.billing_cycle = payload.billing_cycle
    if payload.alert_threshold_pct is not None:
        sub.alert_threshold_pct = payload.alert_threshold_pct

    audit(db, "billing.subscription.update", "organization", str(org.id), user, org.id)
    db.commit()
    spec, _ = plans.effective_plan(db, org)
    return _subscription_dict(org, spec, sub)


class CheckoutSessionInput(BaseModel):
    target_plan: Literal["FREE", "STARTER", "PRO", "PROFESSIONAL", "BUSINESS", "ENTERPRISE"]
    billing_cycle: Literal["monthly", "annual"] = "monthly"
    provider: Literal["sandbox", "stripe", "mercadopago", "transbank"] | None = None
    currency: Literal["USD", "CLP", "UF"] = "USD"
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)


def _plan_amount_cents(spec: plans.PlanSpec, cycle: str, currency: str) -> int:
    price = spec.annual_price_usd if cycle == "annual" else spec.monthly_price_usd
    usd = float(price or 0)
    if currency == "CLP":
        return int(usd * 950)
    if currency == "UF":
        return max(1, round(usd / 40)) if usd > 0 else 0
    return int(usd * 100)


@router.post("/api/v1/billing/checkout")
def create_checkout_session(
    organization_id: str,
    payload: CheckoutSessionInput,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create a provider customer and hosted checkout session with idempotency protection."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.owner)
    org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")

    spec = plans.plan_spec(payload.target_plan)
    provider_name = payload.provider or settings().billing_provider
    idem = payload.idempotency_key or f"chk:{org.id}:{spec.code}:{payload.billing_cycle}:{provider_name}"

    existing = db.scalar(
        select(BillingTransaction).where(
            BillingTransaction.organization_id == org.id,
            BillingTransaction.idempotency_key == idem,
        )
    )
    if existing is not None:
        return {
            "transaction_id": str(existing.id),
            "provider": existing.provider,
            "external_tx_id": existing.external_tx_id,
            "idempotency_key": existing.idempotency_key,
            "target_plan": existing.plan_code,
            "billing_cycle": existing.billing_cycle,
            "amount_cents": existing.amount_cents,
            "currency": existing.currency,
            "status": existing.status,
            "checkout_url": existing.metadata_.get("checkout_url", ""),
            "idempotent_replay": True,
            "live_charges_enabled": bool(settings().billing_live_enabled and existing.provider != "sandbox"),
        }

    try:
        adapter = billing_providers.get_payment_adapter(provider_name)
        sub = _ensure_subscription(db, org)
        if not sub.external_customer_id or sub.payment_provider != adapter.name:
            cust = adapter.create_customer(str(org.id), user.email, org.name)
            sub.external_customer_id = cust.customer_id
            sub.payment_provider = adapter.name
        amount_cents = _plan_amount_cents(spec, payload.billing_cycle, payload.currency)
        base_url = settings().frontend_base_url.rstrip("/")
        chk = adapter.create_checkout(
            organization_id=str(org.id),
            customer_id=sub.external_customer_id,
            plan_code=spec.code,
            billing_cycle=payload.billing_cycle,
            amount_cents=amount_cents,
            currency=payload.currency,
            idempotency_key=idem,
            success_url=f"{base_url}/billing?checkout=success",
            cancel_url=f"{base_url}/billing?checkout=canceled",
        )
    except billing_providers.PaymentProviderError as exc:
        err_tx = BillingTransaction(
            organization_id=org.id,
            provider=provider_name,
            external_tx_id=f"err_{uuid.uuid4().hex[:16]}",
            idempotency_key=idem,
            kind="checkout",
            plan_code=spec.code,
            billing_cycle=payload.billing_cycle,
            amount_cents=0,
            currency=payload.currency,
            status="failed",
            error_code=exc.code,
            metadata_={"retryable": exc.retryable},
        )
        db.add(err_tx)
        db.commit()
        raise HTTPException(503 if exc.retryable else 422, exc.code) from None

    if chk.subscription_id and not sub.external_subscription_id:
        sub.external_subscription_id = chk.subscription_id

    tx = BillingTransaction(
        organization_id=org.id,
        provider=chk.provider,
        external_tx_id=chk.session_id,
        idempotency_key=idem,
        kind="checkout",
        plan_code=spec.code,
        billing_cycle=payload.billing_cycle,
        amount_cents=chk.amount_cents,
        currency=chk.currency,
        status=chk.status,
        metadata_={
            "checkout_url": chk.checkout_url,
            "customer_id": chk.customer_id,
            "subscription_id": chk.subscription_id,
        },
    )
    db.add(tx)
    audit(db, "billing.checkout.create", "organization", str(org.id), user, org.id)
    db.commit()
    return {
        "transaction_id": str(tx.id),
        "provider": tx.provider,
        "external_tx_id": tx.external_tx_id,
        "idempotency_key": tx.idempotency_key,
        "target_plan": tx.plan_code,
        "billing_cycle": tx.billing_cycle,
        "amount_cents": tx.amount_cents,
        "currency": tx.currency,
        "status": tx.status,
        "checkout_url": chk.checkout_url,
        "idempotent_replay": False,
        "live_charges_enabled": chk.live_mode,
    }


class CancelSubscriptionInput(BaseModel):
    immediate: bool = False
    reason: str | None = Field(default=None, max_length=200)


@router.post("/api/v1/billing/subscription/cancel")
def cancel_subscription(
    organization_id: str,
    payload: CancelSubscriptionInput,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.owner)
    org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    sub = _ensure_subscription(db, org)
    adapter = billing_providers.get_payment_adapter(
        sub.payment_provider if sub.payment_provider != "none" else "sandbox"
    )
    try:
        res = adapter.cancel_subscription(
            subscription_id=sub.external_subscription_id or f"sub_local_{org.id}",
            immediate=payload.immediate,
        )
    except billing_providers.PaymentProviderError as exc:
        raise HTTPException(503 if exc.retryable else 422, exc.code) from None

    if payload.immediate:
        sub.status = "canceled"
        sub.cancel_at_period_end = False
        org.plan = plans.db_plan_enum("FREE")
    else:
        sub.cancel_at_period_end = res.cancel_at_period_end
    audit(db, "billing.subscription.cancel", "organization", str(org.id), user, org.id)
    db.commit()
    spec, _ = plans.effective_plan(db, org)
    return _subscription_dict(org, spec, sub)


class RefundInput(BaseModel):
    transaction_id: uuid.UUID
    amount_cents: int | None = Field(default=None, ge=1)
    reason: str = Field(default="requested_by_customer", max_length=120)


@router.post("/api/v1/billing/refund")
def refund_transaction(
    organization_id: str,
    payload: RefundInput,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.owner)
    tx = db.scalar(
        select(BillingTransaction)
        .where(
            BillingTransaction.id == payload.transaction_id,
            BillingTransaction.organization_id == org_id,
        )
        .with_for_update()
    )
    if tx is None:
        raise HTTPException(404, "TRANSACTION_NOT_FOUND")
    if tx.status != "succeeded":
        raise HTTPException(409, "TRANSACTION_NOT_REFUNDABLE")

    refund_amount = payload.amount_cents or tx.amount_cents
    if refund_amount > tx.amount_cents:
        raise HTTPException(422, "REFUND_EXCEEDS_TRANSACTION")

    adapter = billing_providers.get_payment_adapter(tx.provider)
    try:
        ref = adapter.refund(
            external_tx_id=tx.external_tx_id,
            amount_cents=refund_amount,
            currency=tx.currency,
            reason=payload.reason,
        )
    except billing_providers.PaymentProviderError as exc:
        raise HTTPException(503 if exc.retryable else 422, exc.code) from None

    tx.status = "refunded"
    refund_tx = BillingTransaction(
        organization_id=org_id,
        provider=tx.provider,
        external_tx_id=ref.refund_id,
        idempotency_key=f"refund:{tx.id}",
        kind="refund",
        plan_code=tx.plan_code,
        billing_cycle=tx.billing_cycle,
        amount_cents=ref.amount_cents,
        currency=ref.currency,
        status="refunded",
        metadata_={"original_tx_id": str(tx.id), "reason": payload.reason},
    )
    db.add(refund_tx)
    audit(db, "billing.refund", "billing_transaction", str(tx.id), user, org_id)
    db.commit()
    return {
        "refund_transaction_id": str(refund_tx.id),
        "original_transaction_id": str(tx.id),
        "external_refund_id": ref.refund_id,
        "amount_cents": ref.amount_cents,
        "currency": ref.currency,
        "status": ref.status,
    }


@router.get("/api/v1/billing/transactions")
def list_transactions(
    organization_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin, scope="usage:read")
    txs = db.scalars(
        select(BillingTransaction)
        .where(BillingTransaction.organization_id == org_id)
        .order_by(BillingTransaction.created_at.desc())
        .limit(limit)
    ).all()
    events = db.scalars(
        select(BillingWebhookEvent)
        .where(BillingWebhookEvent.organization_id == org_id)
        .order_by(BillingWebhookEvent.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "transactions": [
            {
                "id": str(t.id),
                "provider": t.provider,
                "external_tx_id": t.external_tx_id,
                "kind": t.kind,
                "plan_code": t.plan_code,
                "billing_cycle": t.billing_cycle,
                "amount_cents": t.amount_cents,
                "currency": t.currency,
                "status": t.status,
                "error_code": t.error_code,
                "created_at": t.created_at,
            }
            for t in txs
        ],
        "webhook_events": [
            {
                "id": str(e.id),
                "provider": e.provider,
                "event_id": e.event_id,
                "event_type": e.event_type,
                "status": e.status,
                "created_at": e.created_at,
            }
            for e in events
        ],
    }


@router.post("/api/v1/billing/reconcile")
def reconcile_billing(
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Reconcile subscription state, organization plan enum and transaction history."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.owner)
    org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    sub = _ensure_subscription(db, org)
    expected_enum = (
        plans.db_plan_enum("FREE")
        if sub.status in ("unpaid", "canceled", "expired")
        else plans.db_plan_enum(sub.plan)
    )
    repaired = False
    if org.plan != expected_enum:
        org.plan = expected_enum
        repaired = True
    audit(db, "billing.reconcile", "organization", str(org.id), user, org.id)
    db.commit()
    spec, _ = plans.effective_plan(db, org)
    return {
        "organization_id": str(org.id),
        "repaired": repaired,
        "status": sub.status,
        "effective_plan": spec.code,
        "configured_plan": sub.plan,
    }


@router.post("/api/v1/billing/webhooks/{provider}")
async def receive_billing_webhook(
    provider: Literal["sandbox", "stripe", "mercadopago", "transbank"],
    request: Request,
    x_billing_signature: str | None = Header(default=None),
    stripe_signature: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Verify HMAC signature + replay window, deduplicate event_id, and apply subscription state transitions."""
    raw = await request.body()
    sig = x_billing_signature or stripe_signature or x_signature or ""
    try:
        adapter = billing_providers.get_payment_adapter(provider)
        evt = adapter.verify_and_parse_webhook(raw, sig)
    except billing_providers.WebhookSignatureError as exc:
        raise HTTPException(400, str(exc)) from None
    except billing_providers.PaymentProviderError as exc:
        raise HTTPException(400, exc.code) from None

    org_id = uuid.UUID(evt.organization_id)
    rls.set_context(db, org_id)
    org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")

    existing_evt = db.scalar(
        select(BillingWebhookEvent).where(
            BillingWebhookEvent.provider == evt.provider,
            BillingWebhookEvent.event_id == evt.event_id,
        )
    )
    if existing_evt is not None:
        return {
            "received": True,
            "duplicate": True,
            "event_id": evt.event_id,
            "status": existing_evt.status,
        }

    sub = _ensure_subscription(db, org)
    sub.payment_provider = evt.provider
    if evt.external_customer_id:
        sub.external_customer_id = evt.external_customer_id
    if evt.external_subscription_id:
        sub.external_subscription_id = evt.external_subscription_id
    if evt.billing_cycle in ("monthly", "annual"):
        sub.billing_cycle = evt.billing_cycle

    _, period_start, period_end = plans.billing_period()
    tx_kind = evt.event_type.replace(".", "_")
    tx_status = "succeeded"

    if evt.event_type in ("payment.succeeded", "subscription.created", "subscription.renewed"):
        if evt.plan_code:
            sub.plan = plans.canonical_plan(evt.plan_code)
        sub.status = evt.metadata.get("normalized_status") or "active"
        sub.failed_payment_count = 0
        sub.cancel_at_period_end = False
        sub.current_period_start = period_start
        sub.current_period_end = period_end
        org.plan = plans.db_plan_enum(sub.plan)
    elif evt.event_type == "subscription.plan_changed":
        if evt.plan_code:
            sub.plan = plans.canonical_plan(evt.plan_code)
            org.plan = plans.db_plan_enum(sub.plan)
        sub.status = "active"
    elif evt.event_type == "payment.failed":
        tx_status = "failed"
        sub.failed_payment_count = (sub.failed_payment_count or 0) + 1
        if sub.failed_payment_count >= 3:
            sub.status = "unpaid"
            org.plan = plans.db_plan_enum("FREE")
        else:
            sub.status = "past_due"
    elif evt.event_type == "subscription.canceled":
        sub.status = "canceled"
        sub.cancel_at_period_end = False
        org.plan = plans.db_plan_enum("FREE")
    elif evt.event_type == "payment.refunded":
        tx_status = "refunded"
    elif evt.event_type == "payment_method.updated" and sub.status in ("past_due", "incomplete"):
        sub.status = "active"
        sub.failed_payment_count = 0

    webhook_row = BillingWebhookEvent(
        organization_id=org.id,
        provider=evt.provider,
        event_id=evt.event_id,
        event_type=evt.event_type,
        status="processed",
        payload_hash=hashlib.sha256(raw).hexdigest(),
    )
    db.add(webhook_row)

    idem = f"wh:{evt.provider}:{evt.event_id}"
    if not db.scalar(
        select(BillingTransaction.id).where(
            BillingTransaction.organization_id == org.id,
            BillingTransaction.idempotency_key == idem,
        )
    ):
        db.add(
            BillingTransaction(
                organization_id=org.id,
                provider=evt.provider,
                external_tx_id=evt.external_tx_id or f"tx_{evt.event_id[:24]}",
                idempotency_key=idem,
                kind=tx_kind[:32],
                plan_code=sub.plan,
                billing_cycle=sub.billing_cycle,
                amount_cents=evt.amount_cents,
                currency=evt.currency,
                status=tx_status,
                metadata_={"event_id": evt.event_id, "updated_at": utcnow().isoformat()},
            )
        )

    db.commit()
    return {
        "received": True,
        "duplicate": False,
        "event_id": evt.event_id,
        "event_type": evt.event_type,
        "subscription_status": sub.status,
        "plan": sub.plan,
        "failed_payment_count": sub.failed_payment_count,
    }


@router.get("/api/v1/billing/usage")
def billing_usage(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin, scope="usage:read")
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    return plans.usage_snapshot(db, org)


@router.get("/api/v1/billing/statements")
def billing_statements(
    organization_id: str,
    months: int = Query(default=6, ge=1, le=24),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin, scope="usage:read")
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    return plans.monthly_statements(db, org, months=months)


class CheckoutPreviewInput(BaseModel):
    target_plan: Literal["FREE", "STARTER", "PRO", "PROFESSIONAL", "BUSINESS", "ENTERPRISE"]
    billing_cycle: Literal["monthly", "annual"] = "monthly"


@router.post("/api/v1/billing/checkout-preview")
def checkout_preview(
    organization_id: str,
    payload: CheckoutPreviewInput,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Payment-provider-ready checkout summary without storing card data or triggering live charges."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.owner)
    spec = plans.plan_spec(payload.target_plan)
    amount = spec.annual_price_usd if payload.billing_cycle == "annual" else spec.monthly_price_usd
    return {
        "organization_id": str(org_id),
        "target_plan": spec.code,
        "display_name": spec.display_name,
        "billing_cycle": payload.billing_cycle,
        "estimated_amount_usd": amount,
        "live_charges_enabled": bool(settings().billing_live_enabled),
        "provider_status": "live" if settings().billing_live_enabled else "sandbox_configuration_only",
        "limits": spec.to_dict(),
    }
