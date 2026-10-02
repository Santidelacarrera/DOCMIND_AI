import csv
import hashlib
import io
import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime
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
from app.models import (
    ApiKey,
    AuditLog,
    Document,
    DocumentVersion,
    ExtractionField,
    ExtractionRun,
    ExtractionSchema,
    ExtractionSchemaVersion,
    JobStatus,
    Organization,
    OrganizationMember,
    ProcessingJob,
    Project,
    Role,
    RunStatus,
    UsageRecord,
    User,
)
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
from app.worker import process_document

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _config.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    allow_credentials=True,
    max_age=600,
)

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


class RegistrationInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    organization_name: str = Field(min_length=1, max_length=160)


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(select(1))
    return {"status": "ready"}


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
    version = ExtractionSchemaVersion(schema_id=schema.id, version=1, json_schema=json_schema)
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


@app.post("/api/v1/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    organization_id: str,
    project_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    enforce(request, "upload", settings().rate_limit_upload)
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
    declared = request.headers.get("content-length")
    # Multipart framing adds a small overhead; reject clearly oversized bodies before reading.
    if declared and declared.isdigit() and int(declared) > settings().max_upload_bytes + 65_536:
        raise HTTPException(413, "FILE_TOO_LARGE")
    if not db.scalar(
        select(Project).where(Project.id == project_id, Project.organization_id == org_id)
    ):
        raise HTTPException(404, "PROJECT_NOT_FOUND")
    content = await file.read(settings().max_upload_bytes + 1)
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
        job = ProcessingJob(organization_id=org_id, document_id=document.id, status=JobStatus.queued)
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
        .where(Document.organization_id == org_id)
        .order_by(Document.created_at.desc())
        .limit(100)
    ).all()
    return [
        {
            "id": str(d.id),
            "filename": d.filename,
            "pages": d.page_count,
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
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Queue one logical process at a time, including across concurrent requests."""
    enforce(request, "process", settings().rate_limit_api)
    org_id = globals()["organization_id"](organization_id)
    membership(org_id, user, db, Role.member, scope="documents:write")
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
