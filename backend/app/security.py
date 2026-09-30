import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Cookie, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import settings
from app.db import get_db
from app.models import ApiKey, OrganizationMember, Role, User

password_hash = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class ApiPrincipal:
    key: ApiKey

    @property
    def organization_id(self):
        return self.key.organization_id


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def create_access_token(user: User) -> str:
    return jwt.encode(
        {
            "sub": str(user.id),
            "sv": user.session_version,
            "jti": secrets.token_urlsafe(16),
            "exp": datetime.now(UTC) + timedelta(minutes=settings().access_token_minutes),
        },
        settings().effective_jwt_secret,
        algorithm=settings().jwt_algorithm,
    )


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User | ApiPrincipal:
    token = credentials.credentials if credentials else access_token
    if not token:
        raise HTTPException(401, "AUTH_REQUIRED")
    try:
        subject = jwt.decode(
            token, settings().effective_jwt_secret, algorithms=[settings().jwt_algorithm]
        )
    except jwt.PyJWTError:
        key = lookup_api_key(token, db)
        if not key:
            raise HTTPException(401, "AUTH_REQUIRED")
        return ApiPrincipal(key)
    user = db.get(User, subject["sub"])
    if not user or not user.is_active or subject.get("sv") != user.session_version:
        raise HTTPException(401, "AUTH_REQUIRED")
    return user


def membership(
    org_id, user: User | ApiPrincipal, db: Session, minimum: Role = Role.viewer, scope: str | None = None
) -> OrganizationMember | None:
    if isinstance(user, ApiPrincipal):
        if user.organization_id != org_id:
            raise HTTPException(403, "FORBIDDEN")
        if not scope or scope not in user.key.scopes:
            raise HTTPException(403, "INSUFFICIENT_SCOPE")
        return None
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.user_id == user.id
        )
    )
    levels = {Role.viewer: 0, Role.member: 1, Role.admin: 2, Role.owner: 3}
    if not member or levels[member.role] < levels[minimum]:
        raise HTTPException(403, "FORBIDDEN")
    return member


def issue_api_key() -> tuple[str, str, str]:
    secret = secrets.token_urlsafe(32)
    value = f"dm_test_{secret}"
    return value, value[:12], hashlib.sha256(value.encode()).hexdigest()


def lookup_api_key(token: str, db: Session) -> ApiKey | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == digest))
    if key and not key.revoked_at and (not key.expires_at or key.expires_at > datetime.now(UTC)):
        key.last_used_at = datetime.now(UTC)
        db.commit()
        return key
    return None
