"""Extraction schemas and their versions."""

import json
import logging
import uuid

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import api_support
from app.api_support import MAX_SCHEMA_BYTES, Name, audit
from app.core import settings
from app.db import get_db
from app.models import (
    ExtractionSchema,
    ExtractionSchemaVersion,
    Project,
    Role,
    User,
)
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


@router.post("/api/v1/schemas", status_code=201)
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
    org_id = api_support.organization_id(organization_id)
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
        schema_id=schema.id,
        version=1,
        json_schema=json_schema,
        prompt_instructions=prompt_instructions,
    )
    db.add(version)
    audit(db, "schema.create", "schema", str(schema.id), user, org_id)
    db.commit()
    return {"id": str(schema.id), "version_id": str(version.id), "version": 1}


@router.get("/api/v1/schemas")
def schemas(
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    org_id = api_support.organization_id(organization_id)
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


@router.get("/api/v1/schemas/{schema_id}/versions")
def schema_versions(
    schema_id: uuid.UUID,
    organization_id: str,
    user: User | ApiPrincipal = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    org_id = api_support.organization_id(organization_id)
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


@router.post("/api/v1/schemas/{schema_id}/versions", status_code=201)
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
    org_id = api_support.organization_id(organization_id)
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
