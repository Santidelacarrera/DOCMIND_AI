"""Registration, login, logout, current user and organizations."""

import hashlib
import logging
from secrets import token_urlsafe

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api_support import Name, audit
from app.core import settings
from app.db import get_db
from app.models import (
    Organization,
    OrganizationMember,
    Role,
    User,
)
from app.rate_limit import enforce
from app.security import (
    create_access_token,
    hash_password,
    require_user,
    verify_login_password,
)

logger = logging.getLogger("docmind")
_config = settings()
_production = _config.is_production

router = APIRouter()


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
        "passwordpassword",
        "123456789012",
        "qwertyuiopas",
        "password1234",
        "administrator",
        "letmein123456",
        "iloveyou1234",
        "welcome12345",
    }
)


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)



def set_session_cookies(response: Response, token: str) -> None:
    config = settings()
    response.set_cookie(
        "access_token",
        token,
        httponly=True,
        secure=config.effective_cookie_secure,
        samesite=config.cookie_samesite,
        max_age=config.access_token_minutes * 60,
        path="/",
    )
    response.set_cookie(
        "csrf_token",
        token_urlsafe(32),
        httponly=False,
        secure=config.effective_cookie_secure,
        samesite=config.cookie_samesite,
        max_age=config.access_token_minutes * 60,
        path="/",
    )


@router.post("/api/v1/auth/register", status_code=201)
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


@router.post("/api/v1/auth/login")
def login(
    payload: LoginInput, request: Request, response: Response, db: Session = Depends(get_db)
) -> dict[str, object]:
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


@router.post("/api/v1/auth/logout", status_code=204)
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


@router.get("/api/v1/auth/me")
def me(user: User = Depends(require_user)) -> dict[str, object]:
    return {"id": str(user.id), "email": user.email}


@router.post("/api/v1/organizations", status_code=201)
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


@router.get("/api/v1/organizations")
def organizations(
    user: User = Depends(require_user), db: Session = Depends(get_db)
) -> list[dict[str, str]]:
    return [
        {"id": str(o.id), "name": o.name, "role": m.role.value}
        for o, m in db.execute(
            select(Organization, OrganizationMember)
            .join(OrganizationMember)
            .where(OrganizationMember.user_id == user.id)
        ).all()
    ]
