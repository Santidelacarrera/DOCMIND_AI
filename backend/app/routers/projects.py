"""Projects (cases) and per-project grants."""

import logging
import uuid
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import api_support
from app.api_support import Name, audit
from app.core import settings
from app.db import get_db
from app.models import (
    OrganizationMember,
    Project,
    ProjectMember,
    Role,
    User,
)
from app.project_access import accessible_project_ids
from app.security import (
    ApiPrincipal,
    current_user,
    membership,
    require_user,
)

logger = logging.getLogger("docmind")
_config = settings()
_production = _config.is_production

router = APIRouter()


@router.post("/api/v1/projects", status_code=201)
def create_project(
    organization_id: str,
    name: str = Name,
    description: str | None = Query(default=None, max_length=2000),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.member)
    project = Project(organization_id=org_id, name=name.strip(), description=description)
    if not project.name:
        raise HTTPException(422, "Project name is required")
    db.add(project)
    db.flush()
    audit(db, "project.create", "project", str(project.id), user, org_id)
    db.commit()
    return {"id": str(project.id), "name": project.name}


@router.get("/api/v1/projects")
def projects(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, scope="documents:read")
    # Phase 3C: members and viewers see only the projects they may read; owners and admins see all.
    # Filtered in the query, so hidden projects are neither listed nor counted.
    visible = accessible_project_ids(db, org_id, user)
    return [
        {"id": str(x.id), "name": x.name, "description": x.description}
        for x in db.scalars(
            select(Project).where(Project.organization_id == org_id, Project.id.in_(visible))
        ).all()
    ]


class ProjectMemberInput(BaseModel):
    user_id: uuid.UUID
    access: Literal["read", "write"] = "read"


def _project_in_org(db: Session, project_id: uuid.UUID, org_id: uuid.UUID) -> None:
    if not db.scalar(select(Project.id).where(Project.id == project_id, Project.organization_id == org_id)):
        raise HTTPException(404, "PROJECT_NOT_FOUND")


@router.get("/api/v1/projects/{project_id}/members")
def project_members(
    project_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    """Who has an explicit grant on this project (administrators only; API keys cannot read it)."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    _project_in_org(db, project_id, org_id)
    rows = db.scalars(
        select(ProjectMember).where(ProjectMember.project_id == project_id).order_by(ProjectMember.created_at)
    ).all()
    return [{"user_id": str(r.user_id), "access": r.access} for r in rows]


@router.put("/api/v1/projects/{project_id}/members")
def grant_project_access(
    project_id: uuid.UUID,
    organization_id: str,
    payload: ProjectMemberInput,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Give a person read or write access to one project. Owners and admins already have it."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    _project_in_org(db, project_id, org_id)
    target = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.user_id == payload.user_id
        )
    )
    if target is None:
        raise HTTPException(422, "USER_NOT_IN_ORGANIZATION")
    grant = db.scalar(
        select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == payload.user_id)
    )
    if grant is None:
        db.add(ProjectMember(project_id=project_id, user_id=payload.user_id, access=payload.access, granted_by=user.id))
    else:
        grant.access, grant.granted_by = payload.access, user.id
    db.flush()
    audit(db, "project.member.grant", "project", str(project_id), user, org_id)
    db.commit()
    return {"project_id": str(project_id), "user_id": str(payload.user_id), "access": payload.access}


@router.delete("/api/v1/projects/{project_id}/members/{member_user_id}", status_code=204)
def revoke_project_access(
    project_id: uuid.UUID,
    member_user_id: uuid.UUID,
    organization_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Take a grant away. Effective on the next request: access is checked on every call."""
    org_id = api_support.organization_id(organization_id)
    membership(org_id, user, db, Role.admin)
    _project_in_org(db, project_id, org_id)
    grant = db.scalar(
        select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == member_user_id)
    )
    if grant is None:
        raise HTTPException(404, "GRANT_NOT_FOUND")
    db.delete(grant)
    audit(db, "project.member.revoke", "project", str(project_id), user, org_id)
    db.commit()
    return Response(status_code=204)
