"""Decoupled payment gateway adapters for SaaS billing (Stripe, Mercado Pago, Transbank, Sandbox).

Normalized subscription states:
  trialing | active | past_due | unpaid | canceled | incomplete | expired

Normalized transaction states:
  pending | succeeded | failed | refunded | disputed

Security invariants:
* Card numbers, CVVs and banking credentials never touch DOCMIND servers. Checkout happens on
  the provider's hosted page or tokenized SDK, and DOCMIND stores only opaque provider IDs.
* Payment confirmation is never trusted from the browser: state transitions to ``active`` or
  ``renewed`` require a signed, timestamp-checked, deduplicated provider webhook (or an explicit
  sandbox simulation in non-production / owner sandbox mode).
* Live provider calls are disabled unless ``BILLING_LIVE_ENABLED=true`` and the selected provider's
  credentials are configured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from app.core import settings

SubscriptionStatus = Literal[
    "trialing", "active", "past_due", "unpaid", "canceled", "incomplete", "expired"
]
TransactionStatus = Literal["pending", "succeeded", "failed", "refunded", "disputed"]
ProviderName = Literal["sandbox", "stripe", "mercadopago", "transbank"]

SUBSCRIPTION_STATES: frozenset[str] = frozenset(
    {"trialing", "active", "past_due", "unpaid", "canceled", "incomplete", "expired"}
)
TRANSACTION_STATES: frozenset[str] = frozenset(
    {"pending", "succeeded", "failed", "refunded", "disputed"}
)
WEBHOOK_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "payment.succeeded",
        "payment.failed",
        "subscription.created",
        "subscription.renewed",
        "subscription.canceled",
        "subscription.plan_changed",
        "payment.refunded",
        "payment_method.updated",
    }
)

# Provider-specific status normalization maps
STRIPE_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "unpaid": "unpaid",
    "canceled": "canceled",
    "incomplete": "incomplete",
    "incomplete_expired": "expired",
    "paused": "past_due",
}

MERCADOPAGO_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "authorized": "active",
    "active": "active",
    "pending": "incomplete",
    "paused": "past_due",
    "cancelled": "canceled",
    "canceled": "canceled",
    "expired": "expired",
}

TRANSBANK_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "AUTHORIZED": "active",
    "INITIALIZED": "incomplete",
    "FAILED": "past_due",
    "REJECTED": "past_due",
    "NULLIFIED": "canceled",
    "REVERSED": "canceled",
    "CAPTURED": "active",
}


class PaymentProviderError(RuntimeError):
    """Raised when a payment gateway call fails or is unavailable."""

    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable


class WebhookSignatureError(ValueError):
    """Raised when a webhook signature or timestamp is invalid."""


@dataclass(frozen=True)
class CustomerResult:
    provider: str
    customer_id: str
    email: str


@dataclass(frozen=True)
class CheckoutResult:
    provider: str
    session_id: str
    checkout_url: str
    customer_id: str
    subscription_id: str | None
    amount_cents: int
    currency: str
    status: TransactionStatus
    subscription_status: SubscriptionStatus
    live_mode: bool


@dataclass(frozen=True)
class SubscriptionResult:
    provider: str
    subscription_id: str
    status: SubscriptionStatus
    plan_code: str
    billing_cycle: str
    cancel_at_period_end: bool = False


@dataclass(frozen=True)
class RefundResult:
    provider: str
    refund_id: str
    transaction_id: str
    amount_cents: int
    currency: str
    status: TransactionStatus


@dataclass(frozen=True)
class NormalizedWebhookEvent:
    provider: str
    event_id: str
    event_type: str
    organization_id: str
    plan_code: str | None = None
    billing_cycle: str | None = None
    external_customer_id: str | None = None
    external_subscription_id: str | None = None
    external_tx_id: str | None = None
    amount_cents: int = 0
    currency: str = "USD"
    raw_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PaymentProviderAdapter(Protocol):
    name: str
    supports_native_subscriptions: bool
    supports_refunds: bool
    supported_currencies: tuple[str, ...]

    def create_customer(self, organization_id: str, email: str, name: str) -> CustomerResult: ...

    def create_checkout(
        self,
        *,
        organization_id: str,
        customer_id: str,
        plan_code: str,
        billing_cycle: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutResult: ...

    def update_subscription_plan(
        self,
        *,
        subscription_id: str,
        new_plan_code: str,
        billing_cycle: str,
        amount_cents: int,
        idempotency_key: str,
    ) -> SubscriptionResult: ...

    def cancel_subscription(
        self, *, subscription_id: str, immediate: bool = False
    ) -> SubscriptionResult: ...

    def refund(
        self, *, external_tx_id: str, amount_cents: int, currency: str, reason: str = ""
    ) -> RefundResult: ...

    def normalize_status(self, raw_status: str) -> SubscriptionStatus: ...

    def verify_and_parse_webhook(
        self, payload: bytes, signature_header: str, *, timestamp_header: str | None = None
    ) -> NormalizedWebhookEvent: ...


def _webhook_secret(provider: str) -> str:
    cfg = settings()
    if cfg.billing_webhook_secret and cfg.billing_webhook_secret.get_secret_value():
        return cfg.billing_webhook_secret.get_secret_value()
    if cfg.is_production:
        raise WebhookSignatureError("BILLING_WEBHOOK_SECRET_NOT_CONFIGURED")
    return f"sandbox-webhook-secret-{provider}-{cfg.effective_jwt_secret[:16]}"


def sign_webhook_payload(provider: str, payload: bytes, *, timestamp: int | None = None) -> str:
    """Create a canonical ``t=<unix>,v1=<hex>`` HMAC-SHA256 signature header."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    secret = _webhook_secret(provider).encode("utf-8")
    signed = f"{ts}.".encode() + payload
    digest = hmac.new(secret, signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify_hmac_signature(
    provider: str,
    payload: bytes,
    signature_header: str,
    *,
    tolerance_seconds: int = 300,
) -> int:
    """Verify ``t=<unix>,v1=<hex>`` (Stripe/standard format) or ``ts=<unix>,v1=<hex>`` (MercadoPago)
    and enforce replay window. Returns the verified timestamp."""
    if not signature_header or len(signature_header) > 512:
        raise WebhookSignatureError("MISSING_OR_MALFORMED_SIGNATURE")
    parts: dict[str, str] = {}
    for item in signature_header.split(","):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            parts[k.strip()] = v.strip()
    ts_raw = parts.get("t") or parts.get("ts")
    v1 = parts.get("v1")
    if not ts_raw or not v1:
        raise WebhookSignatureError("INVALID_SIGNATURE_FORMAT")
    try:
        ts = int(ts_raw)
    except ValueError:
        raise WebhookSignatureError("INVALID_SIGNATURE_TIMESTAMP") from None

    now = int(time.time())
    if abs(now - ts) > tolerance_seconds:
        raise WebhookSignatureError("WEBHOOK_TIMESTAMP_OUT_OF_RANGE")

    secret = _webhook_secret(provider).encode("utf-8")
    expected = hmac.new(secret, f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        raise WebhookSignatureError("SIGNATURE_MISMATCH")
    return ts


def _parse_common_event(provider: str, payload: bytes, status_map: dict[str, SubscriptionStatus]) -> NormalizedWebhookEvent:
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WebhookSignatureError("INVALID_WEBHOOK_JSON") from None
    if not isinstance(body, dict):
        raise WebhookSignatureError("INVALID_WEBHOOK_PAYLOAD")

    event_id = str(body.get("event_id") or body.get("id") or "").strip()
    event_type = str(body.get("event_type") or body.get("type") or "").strip()
    org_id = str(body.get("organization_id") or (body.get("data") or {}).get("organization_id") or "").strip()
    if not event_id or len(event_id) > 128:
        raise WebhookSignatureError("MISSING_EVENT_ID")
    if event_type not in WEBHOOK_EVENT_TYPES:
        raise WebhookSignatureError("UNSUPPORTED_WEBHOOK_EVENT")
    try:
        uuid.UUID(org_id)
    except ValueError:
        raise WebhookSignatureError("INVALID_ORGANIZATION_ID") from None

    raw_data = body.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else body
    raw_status = str(data.get("status") or "") if data.get("status") else None
    normalized_status = status_map.get(raw_status, None) if raw_status else None
    meta = dict(data.get("metadata") or {})
    if normalized_status:
        meta["normalized_status"] = normalized_status

    return NormalizedWebhookEvent(
        provider=provider,
        event_id=event_id,
        event_type=event_type,
        organization_id=org_id,
        plan_code=str(data["plan_code"]) if data.get("plan_code") else None,
        billing_cycle=str(data["billing_cycle"]) if data.get("billing_cycle") else None,
        external_customer_id=str(data["customer_id"]) if data.get("customer_id") else None,
        external_subscription_id=str(data["subscription_id"]) if data.get("subscription_id") else None,
        external_tx_id=str(data["transaction_id"]) if data.get("transaction_id") else f"tx_{event_id[:24]}",
        amount_cents=int(data.get("amount_cents") or 0),
        currency=str(data.get("currency") or "USD").upper()[:8],
        raw_status=raw_status,
        metadata=meta,
    )


class _BaseAdapter:
    """Shared deterministic sandbox/adapter behavior; subclasses specialize provider semantics."""

    name: str = "sandbox"
    supports_native_subscriptions: bool = True
    supports_refunds: bool = True
    supported_currencies: tuple[str, ...] = ("USD", "CLP", "UF")
    status_map: dict[str, SubscriptionStatus] = STRIPE_STATUS_MAP
    outage_simulated: bool = False
    http_transport: httpx.BaseTransport | None = None

    def _is_live(self) -> bool:
        return bool(settings().billing_live_enabled and self.name != "sandbox")

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        self._check_available()
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=timeout, transport=self.http_transport) as client:
                    resp = client.request(
                        method,
                        url,
                        headers=headers,
                        json=json_body,
                        data=form_body,
                    )
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < max_retries:
                        continue
                    raise PaymentProviderError(
                        "PAYMENT_PROVIDER_UNAVAILABLE",
                        f"{self.name} HTTP {resp.status_code}",
                        retryable=True,
                    )
                if resp.status_code >= 400:
                    raise PaymentProviderError(
                        "PAYMENT_PROVIDER_REJECTED",
                        f"{self.name} HTTP {resp.status_code}",
                        retryable=False,
                    )
                data = resp.json()
                return data if isinstance(data, dict) else {}
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < max_retries:
                    continue
                raise PaymentProviderError(
                    "PAYMENT_PROVIDER_UNAVAILABLE",
                    f"{self.name} network error: {type(exc).__name__}",
                    retryable=True,
                ) from exc
        raise PaymentProviderError(
            "PAYMENT_PROVIDER_UNAVAILABLE",
            f"{self.name} request failed: {last_exc}",
            retryable=True,
        )

    def _check_available(self) -> None:
        if self.outage_simulated:
            raise PaymentProviderError(
                "PAYMENT_PROVIDER_UNAVAILABLE",
                f"Provider {self.name} is temporarily unreachable",
                retryable=True,
            )

    def create_customer(self, organization_id: str, email: str, name: str) -> CustomerResult:
        self._check_available()
        digest = hashlib.sha256(f"{self.name}:{organization_id}".encode()).hexdigest()[:16]
        return CustomerResult(provider=self.name, customer_id=f"cus_{self.name[:3]}_{digest}", email=email)

    def create_checkout(
        self,
        *,
        organization_id: str,
        customer_id: str,
        plan_code: str,
        billing_cycle: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutResult:
        self._check_available()
        curr = currency.upper()
        if curr not in self.supported_currencies:
            raise PaymentProviderError("UNSUPPORTED_CURRENCY", f"{self.name} does not support {curr}")
        seed = hashlib.sha256(f"{self.name}:{organization_id}:{idempotency_key}".encode()).hexdigest()[:18]
        session_id = f"chk_{self.name[:3]}_{seed}"
        sub_id = f"sub_{self.name[:3]}_{seed}"
        live = bool(settings().billing_live_enabled and self.name != "sandbox")
        checkout_url = f"https://checkout.sandbox.{self.name}.example/s/{session_id}?return={success_url}"
        return CheckoutResult(
            provider=self.name,
            session_id=session_id,
            checkout_url=checkout_url,
            customer_id=customer_id,
            subscription_id=sub_id,
            amount_cents=amount_cents,
            currency=curr,
            status="pending",
            subscription_status="incomplete",
            live_mode=live,
        )

    def update_subscription_plan(
        self,
        *,
        subscription_id: str,
        new_plan_code: str,
        billing_cycle: str,
        amount_cents: int,
        idempotency_key: str,
    ) -> SubscriptionResult:
        self._check_available()
        return SubscriptionResult(
            provider=self.name,
            subscription_id=subscription_id,
            status="active",
            plan_code=new_plan_code,
            billing_cycle=billing_cycle,
            cancel_at_period_end=False,
        )

    def cancel_subscription(
        self, *, subscription_id: str, immediate: bool = False
    ) -> SubscriptionResult:
        self._check_available()
        return SubscriptionResult(
            provider=self.name,
            subscription_id=subscription_id,
            status="canceled" if immediate else "active",
            plan_code="",
            billing_cycle="monthly",
            cancel_at_period_end=not immediate,
        )

    def refund(
        self, *, external_tx_id: str, amount_cents: int, currency: str, reason: str = ""
    ) -> RefundResult:
        self._check_available()
        if not self.supports_refunds:
            raise PaymentProviderError("REFUNDS_NOT_SUPPORTED", f"{self.name} does not support refunds")
        digest = hashlib.sha256(f"re:{self.name}:{external_tx_id}:{amount_cents}".encode()).hexdigest()[:16]
        return RefundResult(
            provider=self.name,
            refund_id=f"re_{self.name[:3]}_{digest}",
            transaction_id=external_tx_id,
            amount_cents=amount_cents,
            currency=currency.upper(),
            status="refunded",
        )

    def normalize_status(self, raw_status: str) -> SubscriptionStatus:
        return self.status_map.get(raw_status, "incomplete")

    def verify_and_parse_webhook(
        self, payload: bytes, signature_header: str, *, timestamp_header: str | None = None
    ) -> NormalizedWebhookEvent:
        verify_hmac_signature(
            self.name,
            payload,
            signature_header,
            tolerance_seconds=settings().billing_webhook_tolerance_seconds,
        )
        return _parse_common_event(self.name, payload, self.status_map)


class SandboxPaymentAdapter(_BaseAdapter):
    name = "sandbox"
    supports_native_subscriptions = True
    supports_refunds = True
    supported_currencies = ("USD", "CLP", "UF")
    status_map = STRIPE_STATUS_MAP


class StripeAdapter(_BaseAdapter):
    """Stripe Billing & Checkout adapter (global USD + CLP cards, native subscriptions & webhooks)."""

    name = "stripe"
    supports_native_subscriptions = True
    supports_refunds = True
    supported_currencies = ("USD", "CLP")
    status_map = STRIPE_STATUS_MAP
    api_base = "https://api.stripe.com/v1"

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        secret = settings().stripe_secret_key
        if secret is None or not secret.get_secret_value():
            raise PaymentProviderError("STRIPE_SECRET_KEY_MISSING", "STRIPE_SECRET_KEY is not configured")
        hdr = {"Authorization": f"Bearer {secret.get_secret_value()}"}
        if idempotency_key:
            hdr["Idempotency-Key"] = idempotency_key
        return hdr

    def create_customer(self, organization_id: str, email: str, name: str) -> CustomerResult:
        if not self._is_live():
            return super().create_customer(organization_id, email, name)
        data = self._request_json(
            "POST",
            f"{self.api_base}/customers",
            headers=self._headers(f"cust-{organization_id}"),
            form_body={"email": email, "name": name, "metadata[organization_id]": organization_id},
        )
        return CustomerResult(provider=self.name, customer_id=str(data["id"]), email=email)

    def create_checkout(
        self,
        *,
        organization_id: str,
        customer_id: str,
        plan_code: str,
        billing_cycle: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutResult:
        if not self._is_live():
            return super().create_checkout(
                organization_id=organization_id,
                customer_id=customer_id,
                plan_code=plan_code,
                billing_cycle=billing_cycle,
                amount_cents=amount_cents,
                currency=currency,
                idempotency_key=idempotency_key,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        curr = currency.upper()
        if curr not in self.supported_currencies:
            raise PaymentProviderError("UNSUPPORTED_CURRENCY", f"stripe does not support {curr}")
        interval = "year" if billing_cycle == "annual" else "month"
        data = self._request_json(
            "POST",
            f"{self.api_base}/checkout/sessions",
            headers=self._headers(idempotency_key),
            form_body={
                "mode": "subscription",
                "customer": customer_id,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "line_items[0][quantity]": "1",
                "line_items[0][price_data][currency]": curr.lower(),
                "line_items[0][price_data][unit_amount]": str(amount_cents),
                "line_items[0][price_data][recurring][interval]": interval,
                "line_items[0][price_data][product_data][name]": f"DocMind AI {plan_code}",
                "metadata[organization_id]": organization_id,
                "metadata[plan_code]": plan_code,
            },
        )
        return CheckoutResult(
            provider=self.name,
            session_id=str(data["id"]),
            checkout_url=str(data.get("url") or ""),
            customer_id=customer_id,
            subscription_id=str(data["subscription"]) if data.get("subscription") else None,
            amount_cents=amount_cents,
            currency=curr,
            status="pending",
            subscription_status="incomplete",
            live_mode=True,
        )

    def cancel_subscription(self, *, subscription_id: str, immediate: bool = False) -> SubscriptionResult:
        if not self._is_live():
            return super().cancel_subscription(subscription_id=subscription_id, immediate=immediate)
        if immediate:
            data = self._request_json(
                "DELETE", f"{self.api_base}/subscriptions/{subscription_id}", headers=self._headers()
            )
        else:
            data = self._request_json(
                "POST",
                f"{self.api_base}/subscriptions/{subscription_id}",
                headers=self._headers(),
                form_body={"cancel_at_period_end": "true"},
            )
        return SubscriptionResult(
            provider=self.name,
            subscription_id=str(data.get("id") or subscription_id),
            status=self.normalize_status(str(data.get("status") or ("canceled" if immediate else "active"))),
            plan_code="",
            billing_cycle="monthly",
            cancel_at_period_end=bool(data.get("cancel_at_period_end", not immediate)),
        )

    def refund(
        self, *, external_tx_id: str, amount_cents: int, currency: str, reason: str = ""
    ) -> RefundResult:
        if not self._is_live():
            return super().refund(
                external_tx_id=external_tx_id, amount_cents=amount_cents, currency=currency, reason=reason
            )
        data = self._request_json(
            "POST",
            f"{self.api_base}/refunds",
            headers=self._headers(f"refund-{external_tx_id}-{amount_cents}"),
            form_body={"payment_intent": external_tx_id, "amount": str(amount_cents)},
        )
        return RefundResult(
            provider=self.name,
            refund_id=str(data["id"]),
            transaction_id=external_tx_id,
            amount_cents=amount_cents,
            currency=currency.upper(),
            status="refunded",
        )


class MercadoPagoAdapter(_BaseAdapter):
    """Mercado Pago Preapproval & Checkout Pro adapter (Chile CLP + LATAM local methods)."""

    name = "mercadopago"
    supports_native_subscriptions = True
    supports_refunds = True
    supported_currencies = ("CLP", "USD")
    status_map = MERCADOPAGO_STATUS_MAP
    api_base = "https://api.mercadopago.com"

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        token = settings().mercadopago_access_token
        if token is None or not token.get_secret_value():
            raise PaymentProviderError("MERCADOPAGO_TOKEN_MISSING", "MERCADOPAGO_ACCESS_TOKEN is not configured")
        hdr = {"Authorization": f"Bearer {token.get_secret_value()}"}
        if idempotency_key:
            hdr["X-Idempotency-Key"] = idempotency_key
        return hdr

    def create_checkout(
        self,
        *,
        organization_id: str,
        customer_id: str,
        plan_code: str,
        billing_cycle: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutResult:
        if not self._is_live():
            return super().create_checkout(
                organization_id=organization_id,
                customer_id=customer_id,
                plan_code=plan_code,
                billing_cycle=billing_cycle,
                amount_cents=amount_cents,
                currency=currency,
                idempotency_key=idempotency_key,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        curr = currency.upper()
        if curr not in self.supported_currencies:
            raise PaymentProviderError("UNSUPPORTED_CURRENCY", f"mercadopago does not support {curr}")
        tx_amount = amount_cents if curr == "CLP" else round(amount_cents / 100, 2)
        data = self._request_json(
            "POST",
            f"{self.api_base}/preapproval",
            headers=self._headers(idempotency_key),
            json_body={
                "reason": f"DocMind AI {plan_code}",
                "external_reference": organization_id,
                "back_url": success_url,
                "auto_recurring": {
                    "frequency": 12 if billing_cycle == "annual" else 1,
                    "frequency_type": "months",
                    "transaction_amount": tx_amount,
                    "currency_id": curr,
                },
            },
        )
        return CheckoutResult(
            provider=self.name,
            session_id=str(data["id"]),
            checkout_url=str(data.get("init_point") or ""),
            customer_id=customer_id,
            subscription_id=str(data["id"]),
            amount_cents=amount_cents,
            currency=curr,
            status="pending",
            subscription_status=self.normalize_status(str(data.get("status") or "pending")),
            live_mode=True,
        )

    def refund(
        self, *, external_tx_id: str, amount_cents: int, currency: str, reason: str = ""
    ) -> RefundResult:
        if not self._is_live():
            return super().refund(
                external_tx_id=external_tx_id, amount_cents=amount_cents, currency=currency, reason=reason
            )
        amount = amount_cents if currency.upper() == "CLP" else round(amount_cents / 100, 2)
        data = self._request_json(
            "POST",
            f"{self.api_base}/v1/payments/{external_tx_id}/refunds",
            headers=self._headers(f"mp-re-{external_tx_id}-{amount_cents}"),
            json_body={"amount": amount},
        )
        return RefundResult(
            provider=self.name,
            refund_id=str(data["id"]),
            transaction_id=external_tx_id,
            amount_cents=amount_cents,
            currency=currency.upper(),
            status="refunded",
        )


class TransbankAdapter(_BaseAdapter):
    """Transbank Webpay Plus / Oneclick Mall adapter (Chile Redcompra debit & credit in CLP/UF).

    Note: Transbank Webpay Plus is transaction-oriented; recurring billing requires Oneclick Mall
    inscription + scheduled merchant-initiated charges rather than provider-hosted subscriptions.
    """

    name = "transbank"
    supports_native_subscriptions = False
    supports_refunds = True
    supported_currencies = ("CLP", "UF")
    status_map = TRANSBANK_STATUS_MAP
    api_base = "https://webpay3g.transbank.cl"

    def _headers(self) -> dict[str, str]:
        key = settings().transbank_api_key
        if key is None or not key.get_secret_value():
            raise PaymentProviderError("TRANSBANK_KEY_MISSING", "TRANSBANK_API_KEY is not configured")
        return {
            "Tbk-Api-Key-Id": settings().transbank_commerce_code or "597055555532",
            "Tbk-Api-Key-Secret": key.get_secret_value(),
        }

    def create_checkout(
        self,
        *,
        organization_id: str,
        customer_id: str,
        plan_code: str,
        billing_cycle: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutResult:
        if not self._is_live():
            return super().create_checkout(
                organization_id=organization_id,
                customer_id=customer_id,
                plan_code=plan_code,
                billing_cycle=billing_cycle,
                amount_cents=amount_cents,
                currency=currency,
                idempotency_key=idempotency_key,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        curr = currency.upper()
        if curr not in self.supported_currencies:
            raise PaymentProviderError("UNSUPPORTED_CURRENCY", f"transbank does not support {curr}")
        buy_order = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        data = self._request_json(
            "POST",
            f"{self.api_base}/rswebpaytransaction/api/webpay/v1.2/transactions",
            headers=self._headers(),
            json_body={
                "buy_order": buy_order,
                "session_id": organization_id[:60],
                "amount": amount_cents,
                "return_url": success_url,
            },
        )
        token = str(data.get("token") or buy_order)
        url = str(data.get("url") or "")
        return CheckoutResult(
            provider=self.name,
            session_id=token,
            checkout_url=f"{url}?token_ws={token}" if url else "",
            customer_id=customer_id,
            subscription_id=None,
            amount_cents=amount_cents,
            currency=curr,
            status="pending",
            subscription_status="incomplete",
            live_mode=True,
        )

    def refund(
        self, *, external_tx_id: str, amount_cents: int, currency: str, reason: str = ""
    ) -> RefundResult:
        if not self._is_live():
            return super().refund(
                external_tx_id=external_tx_id, amount_cents=amount_cents, currency=currency, reason=reason
            )
        data = self._request_json(
            "POST",
            f"{self.api_base}/rswebpaytransaction/api/webpay/v1.2/transactions/{external_tx_id}/refunds",
            headers=self._headers(),
            json_body={"amount": amount_cents},
        )
        return RefundResult(
            provider=self.name,
            refund_id=str(data.get("authorization_code") or f"tbk_re_{external_tx_id[:12]}"),
            transaction_id=external_tx_id,
            amount_cents=amount_cents,
            currency=currency.upper(),
            status="refunded",
        )


_ADAPTERS: dict[str, _BaseAdapter] = {
    "sandbox": SandboxPaymentAdapter(),
    "stripe": StripeAdapter(),
    "mercadopago": MercadoPagoAdapter(),
    "transbank": TransbankAdapter(),
}


def get_payment_adapter(provider: str | None = None) -> _BaseAdapter:
    chosen = (provider or settings().billing_provider or "sandbox").strip().lower()
    adapter = _ADAPTERS.get(chosen)
    if adapter is None:
        raise PaymentProviderError("UNKNOWN_PAYMENT_PROVIDER", f"Unsupported payment provider: {chosen}")
    return adapter


def provider_catalog() -> list[dict[str, Any]]:
    """Capabilities matrix for Chile & international B2B SaaS operation."""
    return [
        {
            "provider": "stripe",
            "display_name": "Stripe Billing & Checkout",
            "recommended_for": "Recommended primary gateway (B2B SaaS global + Chile cards, native subscriptions, dunning, webhooks, USD/CLP)",
            "supports_native_subscriptions": True,
            "supports_refunds": True,
            "supported_currencies": ["USD", "CLP"],
            "chile_notes": "Supports international and Chilean Visa/Mastercard; pairs with DTE/electronic invoicing.",
        },
        {
            "provider": "mercadopago",
            "display_name": "Mercado Pago Suscripciones (Preapproval)",
            "recommended_for": "Recommended secondary gateway for Chile & LATAM local wallets and domestic cards in CLP",
            "supports_native_subscriptions": True,
            "supports_refunds": True,
            "supported_currencies": ["CLP", "USD"],
            "chile_notes": "Native CLP recurring preapprovals and local Chilean payment methods.",
        },
        {
            "provider": "transbank",
            "display_name": "Transbank Webpay Plus / Oneclick Mall",
            "recommended_for": "Enterprise Chilean bank debit (Redcompra) and domestic installment cards via Oneclick Mall",
            "supports_native_subscriptions": False,
            "supports_refunds": True,
            "supported_currencies": ["CLP", "UF"],
            "chile_notes": "Requires Oneclick Mall inscription for recurring merchant-initiated charges; no provider-managed subscription state machine.",
        },
        {
            "provider": "sandbox",
            "display_name": "DOCMIND Deterministic Sandbox",
            "recommended_for": "Local development, automated CI/E2E testing, and dry-run previews without live charges",
            "supports_native_subscriptions": True,
            "supports_refunds": True,
            "supported_currencies": ["USD", "CLP", "UF"],
            "chile_notes": "Zero external network calls; full webhook and state-machine parity.",
        },
    ]
