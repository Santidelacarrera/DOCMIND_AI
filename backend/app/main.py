import csv
import hashlib
import io
import json
import logging
import re
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from secrets import compare_digest, token_urlsafe
from typing import Any
from urllib.parse import quote

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.antivirus import antivirus
from app.core import settings
from app.db import get_db
from app.limits import MaxBodySizeMiddleware
from app.logging_config import configure_logging
from app.metrics import PrometheusMiddleware, metrics_response
from app.models import (
    ApiKey,
    AuditLog,
    Document,
    DocumentVersion,
    ExtractionField,
    ExtractionRun,
    ExtractionSchema,
    ExtractionSchemaVersion,
    Invitation,
    InvitationStatus,
    JobStatus,
    Organization,
    OrganizationMember,
    ProcessingJob,
    Project,
    Role,
    RunStatus,
    UsageRecord,
    User,
    Webhook,
    utcnow,
)
from app.notifications import email_provider, invitation_email
from app.rate_limit import enforce
from app.security import (
    ALLOWED_SCOPES,
    ApiPrincipal,
    create_access_token,
    current_user,
    hash_password,
    issue_api_key,
    membership,
    require_user,
    verify_login_password,
)
from app.storage import storage
from app.telemetry import configure_tracing
from app.webhooks import WebhookURLError, signing_secret
from app.webhooks import validate_url as validate_webhook_url
from app.worker import process_document

configure_logging()
logger = logging.getLogger("docmind")
_config = settings()
_production = _config.environment.lower() in {"staging", "production"}
app = FastAPI(
    title="DocMind AI",
    version="0.1.0",
    # Interactive docs and the schema are not exposed in staging/production.
    docs_url=None if _production else "/docs",
    redoc_url=None if _production else "/redoc",
    openapi_url=None if _production else "/openapi.json",
)
configure_tracing(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _config.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    allow_credentials=True,
    max_age=600,
)
app.add_middleware(PrometheusMiddleware)

# Auth endpoints that establish a session are exempt from CSRF because no session exists yet.
CSRF_EXEMPT_PATHS = {"/api/v1/auth/login", "/api/v1/auth/register"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            settings().csrf_enabled
            and request.url.path not in CSRF_EXEMPT_PATHS
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not request.headers.get("authorization")
            and request.cookies.get("access_token")
        ):
            csrf_cookie = request.cookies.get("csrf_token", "")
            csrf_header = request.headers.get("x-csrf-token", "")
            if not csrf_cookie or not compare_digest(
                csrf_cookie.encode(), csrf_header.encode()
            ):
                return Response(status_code=403, content="CSRF_FAILED")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Resource-Policy"] = "same-site"
        if "Content-Security-Policy" not in response.headers and not request.url.path.startswith(
            ("/docs", "/redoc")
        ):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
            response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        if settings().effective_cookie_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    MaxBodySizeMiddleware,
    upload_limit=settings().max_upload_bytes,
    upload_path="/api/v1/documents",
)


class RegistrationInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    organization_name: str = Field(min_length=1, max_length=160)


def password_problem(password: str, email: str) -> str | None:
    """Cheap guard against the worst choices; length is the main control."""
    lowered = password.lower()
    local = email.split("@", 1)[0].lower()
    if len(set(password)) < 5:
        return "PASSWORD_TOO_SIMPLE"
    if len(local) >= 4 and local in lowered:
        return "PASSWORD_CONTAINS_EMAIL"
    if lowered in COMMON_PASSWORDS:
        return "PASSWORD_TOO_COMMON"
    return None


COMMON_PASSWORDS = frozenset(
    {
        "passwordpassword", "123456789012", "qwertyuiopas", "password1234",
        "administrator", "letmein123456", "iloveyou1234", "welcome12345",
    }
)


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


def audit(
    db: Session,
    action: str,
    target_type: str,
    target_id: str | None,
    actor: User | ApiPrincipal | None = None,
    org_id: uuid.UUID | None = None,
) -> None:
    db.add(
        AuditLog(
            organization_id=org_id,
            actor_id=actor.id if isinstance(actor, User) else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata_={"api_key_id": str(actor.key.id)} if isinstance(actor, ApiPrincipal) else {},
        )
    )


def organization_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(400, "Invalid organization id")


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ \-()\[\]]")


def sanitize_filename(raw: str | None) -> str:
    """Reduce a client-supplied name to a harmless display name (no paths, no control chars)."""
    name = unicodedata.normalize("NFKC", raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip().strip(".")
    return name[:255] or "document.pdf"


def content_disposition(filename: str, inline: bool = False) -> str:
    """RFC 6266 header value that cannot be broken out of by quotes or newlines."""
    ascii_name = _SAFE_NAME.sub("_", filename.encode("ascii", "ignore").decode()) or "document.pdf"
    disposition = "inline" if inline else "attachment"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


_FORMULA_PREFIXES = ("=", "+", "-", "@", chr(9), chr(13), chr(10))


def neutralize_formula(value: object) -> object:
    """Prevent CSV/XLSX formula injection by forcing spreadsheet apps to treat text as text."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


Name = Query(min_length=1, max_length=160)
MAX_SCHEMA_BYTES = 64 * 1024
MAX_FIELD_VALUE_BYTES = 64 * 1024


def live_document(db: Session, org_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    item = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
    )
    if not item:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    return item


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(select(1))
    return {"status": "ready"}


@app.get("/metrics")
def metrics() -> Response:
    # Scraped by Prometheus on the internal network; not tenant data, so it is
    # intentionally unauthenticated like /health. Restrict network access to it
    # at the ingress/load balancer in staging/production.
    return metrics_response()


def set_session_cookies(response: Response, token: str) -> None:
    config = settings()
    response.set_cookie("access_token", token, httponly=True, secure=config.effective_cookie_secure,
                        samesite=config.cookie_samesite, max_age=config.access_token_minutes * 60, path="/")
    response.set_cookie("csrf_token", token_urlsafe(32), httponly=False, secure=config.effective_cookie_secure,
                        samesite=config.cookie_samesite, max_age=config.access_token_minutes * 60, path="/")


@app.post("/api/v1/auth/register", status_code=201)
def register(
    payload: RegistrationInput, request: Request, response: Response, db: Session = Depends(get_db)
) -> dict[str, object]:
    enforce(request, "register", settings().rate_limit_register)
    email = payload.email.lower()
    problem = password_problem(payload.password, email)
    if problem:
        raise HTTPException(422, problem)
    org_name = payload.organization_name.strip()
    if not org_name:
        raise HTTPException(422, "Organization name is required")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "EMAIL_EXISTS")
    user = User(email=email, password_hash=hash_password(payload.password))
    org = Organization(name=org_name)
    db.add_all([user, org])
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "EMAIL_EXISTS") from None
    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=Role.owner))
    audit(db, "register", "user", str(user.id), user, org.id)
    db.commit()
    token = create_access_token(user)
    set_session_cookies(response, token)
    return {
        "access_token": token,
        "token_type": "bearer",
        "organization_id": str(org.id),
    }


@app.post("/api/v1/auth/login")
def login(payload: LoginInput, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, object]:
    email = payload.email.lower()
    enforce(request, "login", settings().rate_limit_login)
    # Per-account bucket slows credential stuffing that rotates source addresses.
    enforce(request, "login-account", settings().rate_limit_login, subject=email)
    user = db.scalar(select(User).where(User.email == email))
    if not verify_login_password(user, payload.password) or user is None:
        # Hash the address so the log is useful for spotting stuffing without storing PII.
        logger.warning(
            "login failed account=%s ip=%s",
            hashlib.sha256(email.encode()).hexdigest()[:12],
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(401, "INVALID_CREDENTIALS")
    audit(db, "login", "user", str(user.id), user)
    db.commit()
    token = create_access_token(user)
    set_session_cookies(response, token)
    return {"access_token": token, "token_type": "bearer"}


@app.post("/api/v1/auth/logout", status_code=204)
def logout(user: User = Depends(require_user), db: Session = Depends(get_db)) -> Response:
    user.session_version += 1
    audit(db, "logout", "user", str(user.id), user)
    db.commit()
    config = settings()
    response = Response(status_code=204)
    for name, http_only in (("access_token", True), ("csrf_token", False)):
        response.delete_cookie(
            name,
            httponly=http_only,
            secure=config.effective_cookie_secure,
            samesite=config.cookie_samesite,
            path="/",
        )
    return response


@app.get("/api/v1/auth/me")
def me(user: User = Depends(require_user)) -> dict[str, object]:
    return {"id": str(user.id), "email": user.email}


@app.post("/api/v1/organizations", status_code=201)
def create_organization(
    name: str = Name, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, str]:
    org = Organization(name=name.strip())
    if not org.name:
        raise HTTPException(422, "Organization name is required")
    db.add(org)
    db.flush()
    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=Role.owner))
    audit(db, "organization.create", "organization", str(org.id), user, org.id)
    db.commit()
    return {"id": str(org.id), "name": org.name}


@app.get("/api/v1/organizations")
def organizations(user: User = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, str]]:
    return [
        {"id": str(o.id), "name": o.name, "role": m.role.value}
        for o, m in db.execute(
            select(Organization, OrganizationMember)
            .join(OrganizationMember)
            .where(OrganizationMember.user_id == user.id)
        ).all()
    ]


ROLE_LEVELS = {Role.viewer: 0, Role.member: 1, Role.admin: 2, Role.owner: 3}


def _member_view(user: User, member: OrganizationMember) -> dict[str, object]:
    return {"user_id": str(user.id), "email": user.email, "role": member.role.value}


def _guard_role_change(actor: OrganizationMember | None, target_role: Role, new_role: Role | None) -> None:
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
            .where(OrganizationMember.organization_id == org_id, OrganizationMember.role == Role.owner)
        )
        or 0
    )


@app.get("/api/v1/members")
def list_members(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.admin)
    rows = db.execute(
        select(User, OrganizationMember)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == org_id)
        .order_by(User.email)
    ).all()
    return [_member_view(u, m) for u, m in rows]


@app.post("/api/v1/members", status_code=201)
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
    org_id = globals()["organization_id"](organization_id)
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


@app.patch("/api/v1/members/{user_id}")
def change_member_role(
    user_id: uuid.UUID,
    organization_id: str,
    role: Role,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = globals()["organization_id"](organization_id)
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


@app.delete("/api/v1/members/{user_id}", status_code=204)
def remove_member(
    user_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = globals()["organization_id"](organization_id)
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


@app.post("/api/v1/invitations", status_code=201)
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
    org_id = globals()["organization_id"](organization_id)
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


@app.get("/api/v1/invitations")
def list_invitations(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.admin)
    return [
        _invitation_view(i)
        for i in db.scalars(
            select(Invitation)
            .where(Invitation.organization_id == org_id)
            .order_by(Invitation.created_at.desc())
        ).all()
    ]


@app.delete("/api/v1/invitations/{invitation_id}", status_code=204)
def revoke_invitation(
    invitation_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.admin)
    invitation = db.scalar(
        select(Invitation).where(Invitation.id == invitation_id, Invitation.organization_id == org_id)
    )
    if not invitation:
        raise HTTPException(404, "INVITATION_NOT_FOUND")
    invitation.status = InvitationStatus.revoked
    audit(db, "invitation.revoke", "invitation", str(invitation.id), user, org_id)
    db.commit()
    return Response(status_code=204)


@app.post("/api/v1/invitations/accept")
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
    if invitation.expires_at < utcnow():
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
    audit(db, "invitation.accept", "invitation", str(invitation.id), user, invitation.organization_id)
    db.commit()
    return {"organization_id": str(invitation.organization_id), "role": invitation.role.value}


@app.post("/api/v1/projects", status_code=201)
def create_project(
    organization_id: str,
    name: str = Name,
    description: str | None = Query(default=None, max_length=2000),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.member)
    project = Project(organization_id=org_id, name=name.strip(), description=description)
    if not project.name:
        raise HTTPException(422, "Project name is required")
    db.add(project)
    db.flush()
    audit(db, "project.create", "project", str(project.id), user, org_id)
    db.commit()
    return {"id": str(project.id), "name": project.name}


@app.get("/api/v1/projects")
def projects(
    organization_id: str, user: User | ApiPrincipal = Depends(current_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="documents:read")
    return [
        {"id": str(x.id), "name": x.name, "description": x.description}
        for x in db.scalars(select(Project).where(Project.organization_id == org_id)).all()
    ]


@app.post("/api/v1/schemas", status_code=201)
def create_schema(
    organization_id: str,
    json_schema: dict[str, object] = Body(...),
    name: str = Name,
    project_id: uuid.UUID | None = None,
    description: str | None = Query(default=None, max_length=2000),
    prompt_instructions: str | None = Query(default=None, max_length=4000),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.member)
    if json_schema.get("type") != "object" or len(json.dumps(json_schema)) > MAX_SCHEMA_BYTES:
        raise HTTPException(422, "SCHEMA_INVALID")
    if not name.strip():
        raise HTTPException(422, "Schema name is required")
    if project_id and not db.scalar(
        select(Project).where(Project.id == project_id, Project.organization_id == org_id)
    ):
        raise HTTPException(404, "PROJECT_NOT_FOUND")
    schema = ExtractionSchema(
        organization_id=org_id,
        project_id=project_id,
        name=name.strip(),
        description=description,
        created_by=user.id,
    )
    db.add(schema)
    db.flush()
    version = ExtractionSchemaVersion(
        schema_id=schema.id, version=1, json_schema=json_schema, prompt_instructions=prompt_instructions
    )
    db.add(version)
    audit(db, "schema.create", "schema", str(schema.id), user, org_id)
    db.commit()
    return {"id": str(schema.id), "version_id": str(version.id), "version": 1}


@app.get("/api/v1/schemas")
def schemas(
    organization_id: str, user: User | ApiPrincipal = Depends(current_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="schemas:read")
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "description": s.description,
            "active": s.active,
            "project_id": str(s.project_id) if s.project_id else None,
        }
        for s in db.scalars(
            select(ExtractionSchema).where(ExtractionSchema.organization_id == org_id)
        ).all()
    ]


def _owned_schema(db: Session, org_id: uuid.UUID, schema_id: uuid.UUID) -> ExtractionSchema:
    schema = db.scalar(
        select(ExtractionSchema).where(
            ExtractionSchema.id == schema_id, ExtractionSchema.organization_id == org_id
        )
    )
    if not schema:
        raise HTTPException(404, "SCHEMA_NOT_FOUND")
    return schema


@app.get("/api/v1/schemas/{schema_id}/versions")
def schema_versions(
    schema_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="schemas:read")
    _owned_schema(db, org_id, schema_id)
    versions = db.scalars(
        select(ExtractionSchemaVersion)
        .where(ExtractionSchemaVersion.schema_id == schema_id)
        .order_by(ExtractionSchemaVersion.version.desc())
    ).all()
    return [
        {
            "id": str(v.id),
            "version": v.version,
            "json_schema": v.json_schema,
            "prompt_instructions": v.prompt_instructions,
            "created_at": v.created_at,
        }
        for v in versions
    ]


@app.post("/api/v1/schemas/{schema_id}/versions", status_code=201)
def create_schema_version(
    schema_id: uuid.UUID,
    organization_id: str,
    json_schema: dict[str, object] = Body(...),
    prompt_instructions: str | None = Query(default=None, max_length=4000),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Publish a new version of an existing schema. Prior versions are kept for
    audit and so in-flight jobs that pinned an old version keep working."""
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.member)
    schema = _owned_schema(db, org_id, schema_id)
    if json_schema.get("type") != "object" or len(json.dumps(json_schema)) > MAX_SCHEMA_BYTES:
        raise HTTPException(422, "SCHEMA_INVALID")
    latest = db.scalar(
        select(func.coalesce(func.max(ExtractionSchemaVersion.version), 0)).where(
            ExtractionSchemaVersion.schema_id == schema_id
        )
    )
    version = ExtractionSchemaVersion(
        schema_id=schema.id,
        version=int(latest or 0) + 1,
        json_schema=json_schema,
        prompt_instructions=prompt_instructions,
    )
    db.add(version)
    db.flush()
    audit(db, "schema.version.create", "schema", str(schema.id), user, org_id)
    db.commit()
    return {"id": str(version.id), "version": version.version}


@app.post("/api/v1/api-keys", status_code=201)
def create_api_key(
    organization_id: str,
    name: str = Name,
    scopes: list[str] = Body(min_length=1, max_length=len(ALLOWED_SCOPES)),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.admin)
    if set(scopes) - ALLOWED_SCOPES:
        raise HTTPException(422, "INVALID_SCOPE")
    scopes = sorted(set(scopes))
    secret, prefix, key_hash = issue_api_key()
    key = ApiKey(
        organization_id=org_id, name=name.strip(), prefix=prefix, key_hash=key_hash, scopes=scopes
    )
    db.add(key)
    db.flush()
    audit(db, "api_key.create", "api_key", str(key.id), user, org_id)
    db.commit()
    return {"id": str(key.id), "key": secret, "prefix": prefix, "scopes": scopes}


@app.get("/api/v1/api-keys")
def list_api_keys(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = globals()["organization_id"](organization_id)
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


@app.delete("/api/v1/api-keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.admin)
    key = db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.organization_id == org_id))
    if not key:
        raise HTTPException(404, "API_KEY_NOT_FOUND")
    key.revoked_at = datetime.now(UTC)
    audit(db, "api_key.revoke", "api_key", str(key.id), user, org_id)
    db.commit()
    return Response(status_code=204)


@app.get("/api/v1/usage")
def usage(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="usage:read")
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


@app.get("/api/v1/audit-logs")
def audit_logs(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = globals()["organization_id"](organization_id)
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


@app.post("/api/v1/webhooks", status_code=201)
def create_webhook(
    organization_id: str,
    payload: WebhookInput,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.admin)
    if not payload.events or set(payload.events) - ALLOWED_WEBHOOK_EVENTS:
        raise HTTPException(422, "INVALID_WEBHOOK_EVENT")
    try:
        validate_webhook_url(payload.url, allow_insecure=settings().environment.lower() == "development")
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


@app.get("/api/v1/webhooks")
def list_webhooks(
    organization_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.admin)
    return [
        _webhook_view(w)
        for w in db.scalars(select(Webhook).where(Webhook.organization_id == org_id)).all()
    ]


@app.delete("/api/v1/webhooks/{webhook_id}", status_code=204)
def delete_webhook(
    webhook_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = globals()["organization_id"](organization_id)
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


def _resolve_requested_schema_version(
    db: Session, org_id: uuid.UUID, schema_id: uuid.UUID | None
) -> ExtractionSchemaVersion | None:
    """Pin the schema's *latest* version at the moment the user picked it, so a
    later edit to the schema doesn't silently retarget an in-flight job."""
    if schema_id is None:
        return None
    schema = db.scalar(
        select(ExtractionSchema).where(
            ExtractionSchema.id == schema_id, ExtractionSchema.organization_id == org_id
        )
    )
    if not schema:
        raise HTTPException(404, "SCHEMA_NOT_FOUND")
    version = db.scalar(
        select(ExtractionSchemaVersion)
        .where(ExtractionSchemaVersion.schema_id == schema.id)
        .order_by(ExtractionSchemaVersion.version.desc())
    )
    if not version:
        raise HTTPException(422, "SCHEMA_HAS_NO_VERSIONS")
    return version


@app.post("/api/v1/documents", status_code=status.HTTP_202_ACCEPTED)
def upload_document(
    organization_id: str,
    project_id: uuid.UUID,
    request: Request,
    schema_id: uuid.UUID | None = None,
    file: UploadFile = File(...),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    enforce(request, "upload", settings().rate_limit_upload)
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    requested_schema_version = _resolve_requested_schema_version(db, org_id, schema_id)
    declared = request.headers.get("content-length")
    # Multipart framing adds a small overhead; reject clearly oversized bodies before reading.
    if declared and declared.isdigit() and int(declared) > settings().max_upload_bytes + 65_536:
        raise HTTPException(413, "FILE_TOO_LARGE")
    if not db.scalar(
        select(Project).where(Project.id == project_id, Project.organization_id == org_id)
    ):
        raise HTTPException(404, "PROJECT_NOT_FOUND")
    content = file.file.read(settings().max_upload_bytes + 1)
    if len(content) > settings().max_upload_bytes:
        raise HTTPException(413, "FILE_TOO_LARGE")
    if not (file.filename or "").lower().endswith(".pdf") or not content.startswith(b"%PDF-"):
        raise HTTPException(415, "UNSUPPORTED_FILE_TYPE")
    try:
        page_count = len(PdfReader(io.BytesIO(content)).pages)
    except Exception:
        raise HTTPException(422, "DOCUMENT_INVALID") from None
    if page_count < 1 or page_count > settings().max_pdf_pages:
        raise HTTPException(422, "PDF_PAGE_LIMIT_EXCEEDED")
    try:
        antivirus().scan(content)
    except RuntimeError as exc:
        raise HTTPException(503 if str(exc) == "ANTIVIRUS_UNAVAILABLE" else 422, str(exc)) from None
    # Lock the organization row so concurrent uploads cannot each observe the
    # same remaining quota and collectively exceed it.
    organization = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    if organization is None:
        raise HTTPException(404, "ORGANIZATION_NOT_FOUND")
    reserved_pages = int(
        db.scalar(
            select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
                UsageRecord.organization_id == org_id, UsageRecord.metric == "pages_reserved"
            )
        )
        or 0
    )
    if organization.plan.value == "FREE" and reserved_pages + page_count > settings().free_pages_per_month:
        raise HTTPException(402, "QUOTA_EXCEEDED")
    checksum = hashlib.sha256(content).hexdigest()
    existing = db.scalar(
        select(Document).where(
            Document.project_id == project_id,
            Document.checksum == checksum,
            Document.deleted_at.is_(None),
        )
    )
    if existing:
        raise HTTPException(409, "DUPLICATE_DOCUMENT")
    key = f"{org_id}/{uuid.uuid4()}.pdf"
    storage.put(key, content)
    try:
        document = Document(
            organization_id=org_id, project_id=project_id, filename=sanitize_filename(file.filename),
            storage_key=key, mime_type="application/pdf", checksum=checksum, page_count=page_count,
        )
        db.add(document)
        db.flush()
        db.add(DocumentVersion(document_id=document.id, storage_key=key, checksum=checksum))
        job = ProcessingJob(
            organization_id=org_id,
            document_id=document.id,
            status=JobStatus.queued,
            requested_schema_version_id=requested_schema_version.id if requested_schema_version else None,
        )
        db.add(job)
        db.add(UsageRecord(organization_id=org_id, metric="pages_reserved", quantity=page_count, document_id=document.id))
        audit(db, "document.upload", "document", str(document.id), user, org_id)
        db.commit()
    except Exception:
        db.rollback()
        storage.delete(key)
        raise
    process_document.delay(str(job.id))
    return {"document_id": str(document.id), "job_id": str(job.id), "status": job.status.value}


@app.get("/api/v1/documents")
def list_documents(
    organization_id: str, user: User | ApiPrincipal = Depends(current_user), db: Session = Depends(get_db)
) -> list[dict[str, object]]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="documents:read")
    docs = db.scalars(
        select(Document)
        .where(Document.organization_id == org_id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
        .limit(100)
    ).all()
    # Latest job per document in one query (avoids N+1).
    latest = (
        select(ProcessingJob.document_id, func.max(ProcessingJob.attempt).label("attempt"))
        .where(ProcessingJob.document_id.in_([d.id for d in docs]))
        .group_by(ProcessingJob.document_id)
        .subquery()
    )
    statuses = {
        doc_id: job_status
        for doc_id, job_status in db.execute(
            select(ProcessingJob.document_id, ProcessingJob.status).join(
                latest,
                (ProcessingJob.document_id == latest.c.document_id)
                & (ProcessingJob.attempt == latest.c.attempt),
            )
        ).all()
    }
    return [
        {
            "id": str(d.id),
            "filename": d.filename,
            "pages": d.page_count,
            "status": statuses[d.id].value if d.id in statuses else None,
            "created_at": d.created_at,
            "project_id": str(d.project_id),
        }
        for d in docs
    ]


@app.post("/api/v1/documents/{document_id}/process", status_code=status.HTTP_202_ACCEPTED)
def process_existing_document(
    document_id: uuid.UUID,
    organization_id: str,
    request: Request,
    schema_id: uuid.UUID | None = None,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Queue one logical process at a time, including across concurrent requests."""
    enforce(request, "process", settings().rate_limit_api)
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    requested_schema_version = _resolve_requested_schema_version(db, org_id, schema_id)
    # The document row lock is the database-level serialization point. It is
    # deliberately not an application-only existence check.
    item = db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if not item:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    active_states = [
        JobStatus.queued,
        JobStatus.processing,
        JobStatus.ocr_processing,
        JobStatus.extracting,
        JobStatus.validating,
    ]
    active = db.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.document_id == item.id,
            ProcessingJob.organization_id == org_id,
            ProcessingJob.status.in_(active_states),
        )
        .order_by(ProcessingJob.created_at.desc())
    )
    if active:
        return {"document_id": str(item.id), "job_id": str(active.id), "status": active.status.value, "reused": True}
    prior_attempt = db.scalar(
        select(func.coalesce(func.max(ProcessingJob.attempt), 0)).where(
            ProcessingJob.document_id == item.id, ProcessingJob.organization_id == org_id
        )
    )
    job = ProcessingJob(
        organization_id=org_id,
        document_id=item.id,
        status=JobStatus.queued,
        attempt=int(prior_attempt or 0) + 1,
        requested_schema_version_id=requested_schema_version.id if requested_schema_version else None,
    )
    db.add(job)
    db.flush()
    audit(db, "document.process", "document", str(item.id), user, org_id)
    db.commit()
    process_document.delay(str(job.id))
    return {"document_id": str(item.id), "job_id": str(job.id), "status": job.status.value, "reused": False}


@app.get("/api/v1/documents/{document_id}/status")
def document_status(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="documents:read")
    live_document(db, org_id, document_id)
    job = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.document_id == document_id, ProcessingJob.organization_id == org_id)
        .order_by(ProcessingJob.created_at.desc())
    )
    if not job:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    return {
        "document_id": str(document_id),
        "status": job.status.value,
        "failure_code": job.failure_code,
    }


@app.get("/api/v1/documents/{document_id}")
def document(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="documents:read")
    item = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
    )
    if not item:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    return {
        "id": str(item.id),
        "filename": item.filename,
        "pages": item.page_count,
        "project_id": str(item.project_id),
        "created_at": item.created_at,
    }


@app.delete("/api/v1/documents/{document_id}", status_code=204)
def delete_document(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Soft-delete the record and remove the stored object (extractions stay for audit)."""
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    item = db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if not item:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    keys = {item.storage_key} | set(
        db.scalars(select(DocumentVersion.storage_key).where(DocumentVersion.document_id == item.id))
    )
    item.deleted_at = datetime.now(UTC)
    audit(db, "document.delete", "document", str(item.id), user, org_id)
    db.commit()
    for key in keys:
        try:
            storage.delete(key)
        except Exception:
            # The record is already gone; the orphaned object is reconciled by operations.
            logger.warning("storage cleanup failed for deleted document %s", document_id)
    return Response(status_code=204)


@app.get("/api/v1/documents/{document_id}/download")
def download(
    document_id: uuid.UUID,
    organization_id: str,
    inline: bool = False,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="documents:read")
    item = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
    )
    if not item:
        raise HTTPException(404, "DOCUMENT_NOT_FOUND")
    frame_ancestors = " ".join(
        o.strip() for o in settings().cors_origins.split(",") if o.strip()
    )
    return Response(
        storage.get(item.storage_key),
        media_type="application/pdf",
        headers={
            "Content-Disposition": content_disposition(item.filename, inline=inline),
            # Only the configured web origins may embed the PDF viewer.
            "Content-Security-Policy": f"default-src 'none'; frame-ancestors {frame_ancestors}",
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/api/v1/documents/{document_id}/extraction")
def extraction(
    document_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="documents:read")
    live_document(db, org_id, document_id)
    run = db.scalar(
        select(ExtractionRun)
        .where(
            ExtractionRun.document_id == document_id,
            ExtractionRun.organization_id == org_id,
            ExtractionRun.status == RunStatus.completed,
        )
        .order_by(ExtractionRun.created_at.desc())
    )
    if not run:
        raise HTTPException(404, "EXTRACTION_NOT_FOUND")
    fields = db.scalars(
        select(ExtractionField).where(ExtractionField.extraction_run_id == run.id)
    ).all()
    return {
        "run_id": str(run.id),
        "result": run.result,
        "requires_review": run.requires_review,
        "validation_issues": run.validation_issues,
        "fields": [
            {
                "id": str(f.id),
                "name": f.name,
                "value": f.value,
                "confidence": f.confidence,
                "manually_verified": f.manually_verified,
            }
            for f in fields
        ],
    }


@app.patch("/api/v1/extraction-fields/{field_id}")
def edit_field(
    field_id: uuid.UUID,
    organization_id: str,
    value: object = Body(...),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    if len(json.dumps(value, default=str)) > MAX_FIELD_VALUE_BYTES:
        raise HTTPException(413, "FIELD_VALUE_TOO_LARGE")
    field = db.scalar(
        select(ExtractionField)
        .join(ExtractionRun)
        .where(ExtractionField.id == field_id, ExtractionRun.organization_id == org_id)
    )
    if not field:
        raise HTTPException(404, "FIELD_NOT_FOUND")
    field.value = value
    field.manually_verified = True
    audit(db, "extraction.edit", "extraction_field", str(field.id), user, org_id)
    db.commit()
    return {"id": str(field.id), "value": field.value, "manually_verified": True}


@app.get("/api/v1/documents/{document_id}/export")
def export(
    document_id: uuid.UUID,
    organization_id: str,
    format: str = Query(default="json", pattern="^(json|csv|xlsx)$"),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, scope="exports:read")
    data: dict[str, Any] = extraction(document_id, organization_id, user, db)
    fields: list[dict[str, Any]] = data["fields"]
    if format == "json":
        return Response(
            json.dumps(data, default=str),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=extraction.json"},
        )
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(
            output, fieldnames=["name", "value", "confidence", "manually_verified"]
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "name": neutralize_formula(f["name"]),
                    "value": neutralize_formula(
                        json.dumps(f["value"]) if isinstance(f["value"], (dict, list)) else f["value"]
                    ),
                    "confidence": f["confidence"],
                    "manually_verified": f["manually_verified"],
                }
                for f in fields
            ]
        )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=extraction.csv"},
        )
    if format == "xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet.title = "Extraction"
        sheet.append(["Field", "Value", "Confidence", "Manually verified"])
        for field in fields:
            value = field["value"]
            sheet.append([
                neutralize_formula(field["name"]),
                neutralize_formula(json.dumps(value) if isinstance(value, (dict, list)) else value),
                field["confidence"],
                field["manually_verified"],
            ])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return Response(
            buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=extraction.xlsx"},
        )
    raise HTTPException(422, "Unsupported export format")
