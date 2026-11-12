"""Organization members and invitations."""

import hashlib
import logging
import uuid
from datetime import timedelta
from secrets import token_urlsafe

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import api_support
from app.api_support import audit
from app.core import settings
from app.db import get_db
from app.models import (
    Invitation,
    InvitationStatus,
    Organization,
    OrganizationMember,
    Role,
    User,
    as_aware_utc,
    utcnow,
)
from app.notifications import email_provider, invitation_email
from app.rate_limit import enforce
from app.security import (
    membership,
    require_user,
)

logger = logging.getLogger("docmind")
_config = settings()
_production = _config.is_production

router = APIRouter()


ROLE_LEVELS = {Role.viewer: 0, Role.member: 1, Role.admin: 2, Role.owner: 3}


def _member_view(user: User, member: OrganizationMember) -> dict[str, object]:
    return {"user_id": str(user.id), "email": user.email, "role": member.role.value}


def _guard_role_change(
    actor: OrganizationMember | None, target_role: Role, new_role: Role | None
) -> None:
    """Admins manage members below them; only owners create, change or remove owners."""
    assert actor is not None  # members endpoints are user-only
    if actor.role != Role.owner and (
        target_role == Role.owner or (new_role is not None and new_role == Role.owner)
    ):
        raise HTTPException(403, "OWNER_REQUIRED")


def _owner_count(db: Session, org_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(OrganizationMember)
            .where(
                OrganizationMember.organization_id == org_id, OrganizationMember.role == Role.owner
            )
        )
        or 0
    )


@router.get("/api/v1/members")
def list_members(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    rows = db.execute(
        select(User, OrganizationMember)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == org_id)
        .order_by(User.email)
    ).all()
    return [_member_view(u, m) for u, m in rows]


@router.post("/api/v1/members", status_code=201)
def add_member(
    request: Request,
    organization_id: str,
    email: EmailStr,
    role: Role = Role.member,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Add an existing account to the organization (no email invitations yet)."""
    enforce(request, "members", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    actor = membership(org_id, user, db, Role.admin)
    _guard_role_change(actor, Role.viewer, role)
    target = db.scalar(select(User).where(User.email == email.lower(), User.is_active.is_(True)))
    if target is None:
        raise HTTPException(404, "USER_NOT_FOUND")
    if db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.user_id == target.id
        )
    ):
        raise HTTPException(409, "ALREADY_MEMBER")
    member = OrganizationMember(organization_id=org_id, user_id=target.id, role=role)
    db.add(member)
    audit(db, "member.add", "user", str(target.id), user, org_id)
    db.commit()
    return _member_view(target, member)


@router.patch("/api/v1/members/{user_id}")
def change_member_role(
    user_id: uuid.UUID,
    organization_id: str,
    role: Role,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = api_support.organization_id(organization_id)
    actor = membership(org_id, user, db, Role.admin)
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.user_id == user_id
        )
    )
    if member is None:
        raise HTTPException(404, "MEMBER_NOT_FOUND")
    _guard_role_change(actor, member.role, role)
    if member.role == Role.owner and role != Role.owner and _owner_count(db, org_id) <= 1:
        raise HTTPException(409, "LAST_OWNER")
    member.role = role
    audit(db, "member.role", "user", str(user_id), user, org_id)
    db.commit()
    target = db.get(User, user_id)
    assert target is not None
    return _member_view(target, member)


@router.delete("/api/v1/members/{user_id}", status_code=204)
def remove_member(
    user_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = api_support.organization_id(organization_id)
    # Anyone may leave; removing someone else needs admin.
    actor = membership(org_id, user, db, Role.viewer if user_id == user.id else Role.admin)
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.user_id == user_id
        )
    )
    if member is None:
        raise HTTPException(404, "MEMBER_NOT_FOUND")
    if user_id != user.id:
        _guard_role_change(actor, member.role, None)
    if member.role == Role.owner and _owner_count(db, org_id) <= 1:
        raise HTTPException(409, "LAST_OWNER")
    db.delete(member)
    audit(db, "member.remove", "user", str(user_id), user, org_id)
    db.commit()
    return Response(status_code=204)


class InvitationInput(BaseModel):
    email: EmailStr
    role: Role = Role.member


def _invitation_view(invitation: Invitation) -> dict[str, object]:
    return {
        "id": str(invitation.id),
        "email": invitation.email,
        "role": invitation.role.value,
        "status": invitation.status.value,
        "expires_at": invitation.expires_at,
        "created_at": invitation.created_at,
    }


@router.post("/api/v1/invitations", status_code=201)
def create_invitation(
    organization_id: str,
    payload: InvitationInput,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Email an existing or not-yet-registered address an invite to join the
    workspace. The raw token is only ever sent by email, never returned here."""
    enforce(request, "invitations", settings().rate_limit_api)
    org_id = api_support.organization_id(organization_id)
    actor = membership(org_id, user, db, Role.admin)
    _guard_role_change(actor, Role.viewer, payload.role)
    email = payload.email.lower()
    existing_member = db.scalar(
        select(OrganizationMember)
        .join(User)
        .where(OrganizationMember.organization_id == org_id, User.email == email)
    )
    if existing_member:
        raise HTTPException(409, "ALREADY_MEMBER")
    pending = db.scalar(
        select(Invitation).where(
            Invitation.organization_id == org_id,
            Invitation.email == email,
            Invitation.status == InvitationStatus.pending,
        )
    )
    if pending:
        raise HTTPException(409, "INVITATION_ALREADY_PENDING")
    token = token_urlsafe(32)
    invitation = Invitation(
        organization_id=org_id,
        email=email,
        role=payload.role,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        invited_by=user.id,
        expires_at=utcnow() + timedelta(hours=settings().invitation_expiry_hours),
    )
    db.add(invitation)
    db.flush()
    organization = db.get(Organization, org_id)
    assert organization is not None
    accept_url = f"{settings().frontend_base_url}/invitations/accept?token={token}"
    subject, body = invitation_email(organization.name, user.email, payload.role.value, accept_url)
    audit(db, "invitation.create", "invitation", str(invitation.id), user, org_id)
    db.commit()
    try:
        email_provider.send(email, subject, body)
    except Exception:
        # The invitation row is already committed; delivery failure is operational,
        # not a reason to fail the request (the admin can see it is still PENDING
        # and the invitee can be nudged through another channel).
        logger.warning("invitation email delivery failed invitation_id=%s", invitation.id)
    return _invitation_view(invitation)


@router.get("/api/v1/invitations")
def list_invitations(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    return [
        _invitation_view(i)
        for i in db.scalars(
            select(Invitation)
            .where(Invitation.organization_id == org_id)
            .order_by(Invitation.created_at.desc())
        ).all()
    ]


@router.delete("/api/v1/invitations/{invitation_id}", status_code=204)
def revoke_invitation(
    invitation_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    invitation = db.scalar(
        select(Invitation).where(
            Invitation.id == invitation_id, Invitation.organization_id == org_id
        )
    )
    if not invitation:
        raise HTTPException(404, "INVITATION_NOT_FOUND")
    invitation.status = InvitationStatus.revoked
    audit(db, "invitation.revoke", "invitation", str(invitation.id), user, org_id)
    db.commit()
    return Response(status_code=204)


@router.post("/api/v1/invitations/accept")
def accept_invitation(
    token: str = Body(..., embed=True, max_length=256),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """The invitee must already be logged in (register or log in first) with the
    exact email address the invitation was sent to."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == token_hash))
    if not invitation or invitation.status != InvitationStatus.pending:
        raise HTTPException(404, "INVITATION_NOT_FOUND")
    if as_aware_utc(invitation.expires_at) < utcnow():
        raise HTTPException(410, "INVITATION_EXPIRED")
    if invitation.email != user.email:
        raise HTTPException(403, "INVITATION_EMAIL_MISMATCH")
    existing = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == invitation.organization_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if not existing:
        db.add(
            OrganizationMember(
                organization_id=invitation.organization_id, user_id=user.id, role=invitation.role
            )
        )
    invitation.status = InvitationStatus.accepted
    invitation.accepted_at = utcnow()
    audit(
        db, "invitation.accept", "invitation", str(invitation.id), user, invitation.organization_id
    )
    db.commit()
    return {"organization_id": str(invitation.organization_id), "role": invitation.role.value}
