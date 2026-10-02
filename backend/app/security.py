import hashlib
import secrets
import uuid
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

# Verified against when the account does not exist so that response time does not
# reveal which email addresses are registered.
_DUMMY_HASH = password_hash.hash(secrets.token_urlsafe(16))

API_KEY_PREFIX = "dm_live_"
LEGACY_API_KEY_PREFIX = "dm_test_"  # keys issued before the prefix rename remain valid
ALLOWED_SCOPES = frozenset(
    {
        "documents:read",
        "documents:write",
        "schemas:read",
        "exports:read",
        "usage:read",
    }
)


@dataclass(frozen=True)
class ApiPrincipal:
    key: ApiKey

    @property
    def organization_id(self) -> uuid.UUID:
        return self.key.organization_id


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def verify_login_password(user: User | None, password: str) -> bool:
    """Constant-work password check that is safe for unknown users and inactive accounts."""
    if user is None:
        password_hash.verify(password, _DUMMY_HASH)
        return False
    return verify_password(password, user.password_hash) and user.is_active


def create_access_token(user: User) -> str:
    return jwt.encode(
        {
            "sub": str(user.id),
            "sv": user.session_version,
            "jti": secrets.token_urlsafe(16),
            "iat": datetime.now(UTC),
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
    if not token or len(token) > 4096:
        raise HTTPException(401, "AUTH_REQUIRED")
    if token.startswith((API_KEY_PREFIX, LEGACY_API_KEY_PREFIX)):
        key = lookup_api_key(token, db)
        if not key:
            raise HTTPException(401, "AUTH_REQUIRED")
        return ApiPrincipal(key)
    try:
        claims = jwt.decode(
            token,
            settings().effective_jwt_secret,
            algorithms=[settings().jwt_algorithm],
            options={"require": ["exp", "sub", "sv"]},
        )
        user_id = uuid.UUID(str(claims["sub"]))
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(401, "AUTH_REQUIRED") from None
    user = db.get(User, user_id)
    if not user or not user.is_active or claims.get("sv") != user.session_version:
        raise HTTPException(401, "AUTH_REQUIRED")
    return user


def require_user(principal: User | ApiPrincipal = Depends(current_user)) -> User:
    """Endpoints that act on behalf of a person (not an API key) depend on this."""
    if isinstance(principal, ApiPrincipal):
        raise HTTPException(403, "FORBIDDEN")
    return principal


_ROLE_LEVELS = {Role.viewer: 0, Role.member: 1, Role.admin: 2, Role.owner: 3}


def membership(
    org_id: uuid.UUID,
    user: User | ApiPrincipal,
    db: Session,
    minimum: Role = Role.viewer,
    scope: str | None = None,
) -> OrganizationMember | None:
    if isinstance(user, ApiPrincipal):
        if user.organization_id != org_id:
            raise HTTPException(403, "FORBIDDEN")
        if not scope or scope not in (user.key.scopes or []):
            raise HTTPException(403, "INSUFFICIENT_SCOPE")
        return None
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.user_id == user.id
        )
    )
    if not member or _ROLE_LEVELS[member.role] < _ROLE_LEVELS[minimum]:
        raise HTTPException(403, "FORBIDDEN")
    return member


def issue_api_key() -> tuple[str, str, str]:
    value = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return value, value[:12], hashlib.sha256(value.encode()).hexdigest()


def lookup_api_key(token: str, db: Session) -> ApiKey | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == digest))
    if key and not key.revoked_at and (not key.expires_at or key.expires_at > datetime.now(UTC)):
        key.last_used_at = datetime.now(UTC)
        db.commit()
        return key
    return None
