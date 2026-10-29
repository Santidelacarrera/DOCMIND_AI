"""Per-project authorization (phase 3B).

Organization membership is still checked by :func:`app.security.membership`. This module adds the
project dimension on top of it:

* **People.** Owners and admins of the organization can act on every project of it. Members and
  viewers need an explicit grant in ``project_members`` (``read`` or ``write``). Revoking a grant
  takes effect on the next request: nothing is cached.
* **API keys.** ``ApiKey.project_ids`` is the list of projects the key may act on. ``NULL`` is a
  legacy organization-wide key: it keeps working on organization-level endpoints but is refused by
  project endpoints, which need an explicit list. Read and write stay separate through the scopes
  (``documents:read`` / ``documents:write``), checked by ``membership``.

A project the caller may not see gets the same 404 as a project that does not exist, so the
response never reveals whether another project exists.
"""

import uuid
from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, OrganizationMember, Project, ProjectMember, Role, User
from app.security import ApiPrincipal

_ORG_WIDE_ROLES = frozenset({Role.owner, Role.admin})


def _org_projects(db: Session, org_id: uuid.UUID) -> list[uuid.UUID]:
    return list(db.scalars(select(Project.id).where(Project.organization_id == org_id)).all())


# What the caller may do on one project. "full" is an owner/admin or a key listing the project (the
# key's scopes still decide read versus write). "write" and "read" come from a person's grant.
FULL, WRITE, READ = "full", "write", "read"


def project_level(db: Session, org_id: uuid.UUID, principal: User | ApiPrincipal, project_id: uuid.UUID) -> str | None:
    """The caller's access to one project of this organization, or None. Call after ``membership``."""
    if not db.scalar(select(Project.id).where(Project.id == project_id, Project.organization_id == org_id)):
        return None
    if isinstance(principal, ApiPrincipal):
        allowed = principal.key.project_ids
        if allowed is None:  # legacy key: organization-wide, as before phase 3B (case endpoints refuse it)
            return FULL
        return FULL if str(project_id) in allowed else None
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.user_id == principal.id
        )
    )
    if member is None:
        return None
    if member.role in _ORG_WIDE_ROLES:
        return FULL
    grant = db.scalar(
        select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == principal.id)
    )
    if grant is None:
        return None
    return WRITE if grant.access == "write" else READ


def require_project(
    db: Session, org_id: uuid.UUID, principal: User | ApiPrincipal, project_id: uuid.UUID, *, write: bool = False
) -> None:
    """Raise unless the caller may read (or, with ``write=True``, change) this project.

    Every refusal that the project itself can cause is a 404, the same as a missing project. A reader
    who asks to change something gets 403: it can already see the project, so that reveals nothing new.
    """
    level = project_level(db, org_id, principal, project_id)
    if level is None:
        raise HTTPException(404, "PROJECT_NOT_FOUND")
    if write and level == READ:
        raise HTTPException(403, "PROJECT_WRITE_DENIED")


def require_document(
    db: Session,
    org_id: uuid.UUID,
    principal: User | ApiPrincipal,
    document_id: uuid.UUID,
    *,
    write: bool = False,
    not_found: str = "DOCUMENT_NOT_FOUND",
) -> Document:
    """Load a live document of this organization and check the caller's access to its project.

    The project is taken from the document on the server, never from the request. A document the caller
    may not see returns ``not_found``, the same as a missing one. ``not_found`` lets an endpoint use its own
    code (for example ``FIELD_NOT_FOUND`` when the document is reached through a field).
    """
    item = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == org_id,
            Document.deleted_at.is_(None),
        )
    )
    level = project_level(db, org_id, principal, item.project_id) if item is not None else None
    if item is None or level is None:
        raise HTTPException(404, not_found)
    if write and level == READ:
        raise HTTPException(403, "PROJECT_WRITE_DENIED")
    return item


def accessible_project_ids(db: Session, org_id: uuid.UUID, principal: User | ApiPrincipal) -> list[uuid.UUID]:
    """Every project of this organization the caller may read. Used by organization-wide queries."""
    if isinstance(principal, ApiPrincipal):
        if principal.key.project_ids is None:  # legacy key: organization-wide, as before this phase
            return _org_projects(db, org_id)
        wanted = {uuid.UUID(p) for p in principal.key.project_ids}
        return [p for p in _org_projects(db, org_id) if p in wanted]
    member = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.user_id == principal.id
        )
    )
    if member is None:
        return []
    if member.role in _ORG_WIDE_ROLES:
        return _org_projects(db, org_id)
    granted = db.scalars(select(ProjectMember.project_id).where(ProjectMember.user_id == principal.id)).all()
    org_projects = set(_org_projects(db, org_id))
    return [p for p in granted if p in org_projects]


def as_strings(ids: Sequence[uuid.UUID]) -> list[str]:
    return [str(i) for i in ids]
