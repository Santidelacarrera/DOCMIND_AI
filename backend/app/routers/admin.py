"""API keys, usage, ContextRAG outbox, audit logs and webhooks."""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Response,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import api_support
from app.api_support import Name, audit
from app.core import settings
from app.db import get_db
from app.models import (
    ApiKey,
    AuditLog,
    Organization,
    Project,
    Role,
    UsageRecord,
    User,
    Webhook,
)
from app.security import (
    ALLOWED_SCOPES,
    ApiPrincipal,
    current_user,
    issue_api_key,
    membership,
    require_user,
)
from app.webhooks import WebhookURLError, signing_secret
from app.webhooks import validate_url as validate_webhook_url

logger = logging.getLogger("docmind")
_config = settings()
_production = _config.is_production

router = APIRouter()


@router.post("/api/v1/api-keys", status_code=201)
def create_api_key(
    organization_id: str,
    name: str = Name,
    scopes: list[str] = Body(min_length=1, max_length=len(ALLOWED_SCOPES)),
    # Repeat the parameter to limit the key to those projects. Without it the key is organization-wide
    # (legacy behaviour): it works on organization endpoints but is refused by the project endpoints.
    project_id: list[uuid.UUID] | None = Query(default=None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    if set(scopes) - ALLOWED_SCOPES:
        raise HTTPException(422, "INVALID_SCOPE")
    scopes = sorted(set(scopes))
    project_ids: list[str] | None = None
    if project_id is not None:
        for pid in set(project_id):
            if not db.scalar(select(Project.id).where(Project.id == pid, Project.organization_id == org_id)):
                raise HTTPException(404, "PROJECT_NOT_FOUND")
        project_ids = sorted(str(pid) for pid in set(project_id))
    secret, prefix, key_hash = issue_api_key()
    key = ApiKey(
        organization_id=org_id, name=name.strip(), prefix=prefix, key_hash=key_hash, scopes=scopes,
        project_ids=project_ids,
    )
    db.add(key)
    db.flush()
    audit(db, "api_key.create", "api_key", str(key.id), user, org_id)
    db.commit()
    return {"id": str(key.id), "key": secret, "prefix": prefix, "scopes": scopes, "project_ids": project_ids}


@router.get("/api/v1/api-keys")
def list_api_keys(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    return [
        {
            "id": str(k.id),
            "name": k.name,
            "prefix": k.prefix,
            "scopes": k.scopes,
            "last_used_at": k.last_used_at,
            "revoked_at": k.revoked_at,
        }
        for k in db.scalars(select(ApiKey).where(ApiKey.organization_id == org_id)).all()
    ]


@router.delete("/api/v1/api-keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    key = db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.organization_id == org_id))
    if not key:
        raise HTTPException(404, "API_KEY_NOT_FOUND")
    key.revoked_at = datetime.now(UTC)
    audit(db, "api_key.revoke", "api_key", str(key.id), user, org_id)
    db.commit()
    return Response(status_code=204)


@router.get("/api/v1/usage")
def usage(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin, scope="usage:read")
    rows = db.execute(
        select(UsageRecord.metric, func.sum(UsageRecord.quantity))
        .where(UsageRecord.organization_id == org_id)
        .group_by(UsageRecord.metric)
    ).all()
    organization = db.get(Organization, org_id)
    if organization is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    return {
        "plan": organization.plan.value,
        "metrics": {metric: quantity for metric, quantity in rows},
        "free_pages_limit": settings().free_pages_per_month,
    }



@router.get("/api/v1/audit-logs")
def audit_logs(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    return [
        {
            "action": x.action,
            "target_type": x.target_type,
            "target_id": x.target_id,
            "created_at": x.created_at,
        }
        for x in db.scalars(
            select(AuditLog)
            .where(AuditLog.organization_id == org_id)
            .order_by(AuditLog.created_at.desc())
            .limit(100)
        ).all()
    ]


class WebhookInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    description: str | None = Field(default=None, max_length=160)
    events: list[str] = Field(default_factory=lambda: ["document.completed"])


ALLOWED_WEBHOOK_EVENTS = frozenset({"document.completed", "document.failed"})


def _webhook_view(webhook: Webhook) -> dict[str, object]:
    return {
        "id": str(webhook.id),
        "url": webhook.url,
        "description": webhook.description,
        "events": webhook.events,
        "active": webhook.active,
        "last_delivery_at": webhook.last_delivery_at,
        "last_delivery_status": webhook.last_delivery_status,
        "created_at": webhook.created_at,
    }


@router.post("/api/v1/webhooks", status_code=201)
def create_webhook(
    organization_id: str,
    payload: WebhookInput,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    if not payload.events or set(payload.events) - ALLOWED_WEBHOOK_EVENTS:
        raise HTTPException(422, "INVALID_WEBHOOK_EVENT")
    try:
        validate_webhook_url(
            payload.url, allow_insecure=settings().environment.lower() == "development"
        )
    except WebhookURLError as exc:
        raise HTTPException(422, str(exc)) from None
    webhook = Webhook(
        organization_id=org_id,
        url=payload.url,
        description=payload.description,
        events=sorted(set(payload.events)),
        created_by=user.id,
    )
    db.add(webhook)
    db.flush()
    audit(db, "webhook.create", "webhook", str(webhook.id), user, org_id)
    db.commit()
    # The signing secret is derived, never stored (see app.webhooks); this is the
    # only moment it is revealed to the caller.
    return {**_webhook_view(webhook), "signing_secret": signing_secret(webhook.id)}


@router.get("/api/v1/webhooks")
def list_webhooks(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    return [
        _webhook_view(w)
        for w in db.scalars(select(Webhook).where(Webhook.organization_id == org_id)).all()
    ]


@router.delete("/api/v1/webhooks/{webhook_id}", status_code=204)
def delete_webhook(
    webhook_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    webhook = db.scalar(
        select(Webhook).where(Webhook.id == webhook_id, Webhook.organization_id == org_id)
    )
    if not webhook:
        raise HTTPException(404, "WEBHOOK_NOT_FOUND")
    db.delete(webhook)
    audit(db, "webhook.delete", "webhook", str(webhook_id), user, org_id)
    db.commit()
    return Response(status_code=204)
