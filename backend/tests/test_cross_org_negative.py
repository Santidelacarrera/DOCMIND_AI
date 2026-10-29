"""Negative cross-organization tests: a legitimate member of one organization must never reach
another organization's objects, even by presenting real identifiers.

The hard case is a *double member* (a user who belongs to both organizations): their membership
check passes for each organization, so isolation depends on every object lookup also filtering by
the organization in the URL. Each route below is exercised with the other organization's ids.
"""

import uuid

import pytest

from app import worker
from app.db import SessionLocal
from app.models import ExtractionField, OrganizationMember, Role
from tests.conftest import make_pdf


def _uid(acct) -> str:
    return acct.client.get("/api/v1/auth/me", headers=acct.headers).json()["id"]


def _join(acct_member, org_id: str, role: Role = Role.member) -> None:
    """Make the user a member of another organization (test setup; the API path is an invitation)."""
    with SessionLocal() as db:
        db.add(
            OrganizationMember(
                organization_id=uuid.UUID(org_id), user_id=uuid.UUID(_uid(acct_member)), role=role
            )
        )
        db.commit()


@pytest.fixture
def worlds(register, client, monkeypatch):
    """Organization A owns a processed document (with fields); B is a separate organization.
    ``double`` belongs to both organizations; ``outsider`` belongs only to B."""
    a, b = register("orgA"), register("orgB")
    project_a = a.project("pa")
    b.project("pb")
    upload_a = a.upload(project_a, make_pdf(1), "a-secret.pdf").json()
    worker.process_document.run(upload_a["job_id"])  # type: ignore[attr-defined]
    doc_a = upload_a["document_id"]
    with SessionLocal() as db:
        from sqlalchemy import select

        from app.models import ExtractionRun

        run = db.scalar(select(ExtractionRun).where(ExtractionRun.document_id == uuid.UUID(doc_a)))
        assert run is not None
        field_a = db.scalar(
            select(ExtractionField).where(ExtractionField.extraction_run_id == run.id)
        )
        assert field_a is not None
        field_a_id = str(field_a.id)
    double = register("double")
    _join(double, b.org)  # double is a member of B ...
    double_in_a = (
        client.post(  # ... and of A (invited through the API by A's owner would be the normal path)
            f"/api/v1/invitations?organization_id={a.org}",
            json={"email": double.email, "role": "MEMBER"},
            headers=a.headers,
        )
    )
    assert double_in_a.status_code in (200, 201)
    with SessionLocal() as db:
        db.add(
            OrganizationMember(
                organization_id=uuid.UUID(a.org), user_id=uuid.UUID(_uid(double)), role=Role.member
            )
        )
        db.commit()
    # Phase 3C: a member reads a document only through a project grant, so A's owner grants read access.
    grant = client.put(
        f"/api/v1/projects/{project_a}/members?organization_id={a.org}",
        json={"user_id": _uid(double), "access": "read"},
        headers=a.headers,
    )
    assert grant.status_code == 200, grant.text
    outsider = register("outsider")
    _join(outsider, b.org)
    return {
        "a": a,
        "b": b,
        "double": double,
        "outsider": outsider,
        "doc": doc_a,
        "field": field_a_id,
        "client": client,
    }


# Every route that takes an organization and an object id.
def _object_routes(doc: str, field: str) -> list[tuple[str, str, object | None]]:
    return [
        ("GET", f"/api/v1/documents/{doc}", None),
        ("GET", f"/api/v1/documents/{doc}/status", None),
        ("GET", f"/api/v1/documents/{doc}/download", None),
        ("GET", f"/api/v1/documents/{doc}/extraction", None),
        ("GET", f"/api/v1/documents/{doc}/export?format=json", None),
        ("GET", f"/api/v1/documents/{doc}/export?format=csv", None),
        ("POST", f"/api/v1/documents/{doc}/process", None),
        ("POST", f"/api/v1/documents/{doc}/review/approve", None),
        ("DELETE", f"/api/v1/documents/{doc}", None),
        ("PATCH", f"/api/v1/extraction-fields/{field}", "forged"),
    ]


def _send(client, method: str, path: str, headers: dict, org: str, body: object | None):
    sep = "&" if "?" in path else "?"
    url = f"{path}{sep}organization_id={org}"
    kwargs: dict = {
        "headers": {**headers, **({"Content-Type": "application/json"} if body is not None else {})}
    }
    if body is not None:
        kwargs["content"] = __import__("json").dumps(body)
    return client.request(method, url, **kwargs)


def test_double_member_cannot_reach_another_orgs_objects_through_the_other_org(worlds) -> None:
    """The double member asks organization B for organization A's document and field ids."""
    client = worlds["client"]
    double = worlds["double"]
    for method, path, body in _object_routes(worlds["doc"], worlds["field"]):
        response = _send(client, method, path, double.headers, worlds["b"].org, body)
        assert response.status_code in (403, 404), (method, path, response.status_code)
        assert b"forged" not in response.content
        assert response.status_code != 200, (method, path)


def test_double_member_sees_a_document_only_under_its_own_organization(worlds) -> None:
    client = worlds["client"]
    double = worlds["double"]
    ok = _send(
        client, "GET", f"/api/v1/documents/{worlds['doc']}", double.headers, worlds["a"].org, None
    )
    assert ok.status_code == 200
    other = _send(
        client, "GET", f"/api/v1/documents/{worlds['doc']}", double.headers, worlds["b"].org, None
    )
    assert other.status_code == 404


def test_single_member_of_b_is_refused_at_the_membership_check(worlds) -> None:
    client = worlds["client"]
    outsider = worlds["outsider"]
    for method, path, body in _object_routes(worlds["doc"], worlds["field"]):
        response = _send(client, method, path, outsider.headers, worlds["a"].org, body)
        assert response.status_code in (403, 404), (method, path, response.status_code)


def test_foreign_project_and_schema_ids_are_refused_on_upload_and_process(worlds) -> None:
    client = worlds["client"]
    b = worlds["b"]
    a = worlds["a"]
    project_a = a.project("pa-second")
    upload = client.post(
        f"/api/v1/documents?organization_id={b.org}&project_id={project_a}",
        files={"file": ("x.pdf", make_pdf(1), "application/pdf")},
        headers=b.headers,
    )
    assert upload.status_code == 404, upload.text
    schema_a = client.post(
        f"/api/v1/schemas?organization_id={a.org}&name=a-only",
        json={"type": "object", "properties": {"x": {}}},
        headers=a.headers,
    ).json()["id"]
    own = b.upload(b.project("pb-2"), make_pdf(1)).json()
    process = client.post(
        f"/api/v1/documents/{own['document_id']}/process?organization_id={b.org}&schema_id={schema_a}",
        headers=b.headers,
    )
    assert process.status_code == 404, process.text


def test_api_key_of_one_organization_cannot_act_in_another(worlds) -> None:
    client = worlds["client"]
    a, b = worlds["a"], worlds["b"]
    key = client.post(
        f"/api/v1/api-keys?organization_id={b.org}&name=b-reader",
        json=["documents:read"],
        headers=b.headers,
    ).json()
    key_headers = {"Authorization": f"Bearer {key['key']}"}
    response = client.get(
        f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=key_headers
    )
    assert response.status_code == 403
    response = client.get(
        f"/api/v1/documents/{worlds['doc']}/extraction?organization_id={a.org}", headers=key_headers
    )
    assert response.status_code == 403


def test_revoked_api_key_stops_working_immediately(worlds) -> None:
    client = worlds["client"]
    a = worlds["a"]
    key = client.post(
        f"/api/v1/api-keys?organization_id={a.org}&name=temp",
        json=["documents:read"],
        headers=a.headers,
    ).json()
    key_headers = {"Authorization": f"Bearer {key['key']}"}
    assert (
        client.get(
            f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=key_headers
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/v1/api-keys/{key['id']}?organization_id={a.org}", headers=a.headers
        ).status_code
        == 204
    )
    assert (
        client.get(
            f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=key_headers
        ).status_code
        == 401
    )


def test_removed_member_loses_access_even_with_a_valid_token(worlds) -> None:
    client = worlds["client"]
    a, double = worlds["a"], worlds["double"]
    assert (
        client.get(
            f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=double.headers
        ).status_code
        == 200
    )
    removed = client.delete(
        f"/api/v1/members/{_uid(double)}?organization_id={a.org}", headers=a.headers
    )
    assert removed.status_code == 204, removed.text
    response = client.get(
        f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=double.headers
    )
    assert response.status_code in (403, 404)
    assert client.get(
        f"/api/v1/documents/{worlds['doc']}/download?organization_id={a.org}",
        headers=double.headers,
    ).status_code in (403, 404)


def test_logout_invalidates_tokens_issued_before_it(worlds) -> None:
    client = worlds["client"]
    a = worlds["a"]
    old = a.headers
    assert (
        client.get(
            f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=old
        ).status_code
        == 200
    )
    assert client.post("/api/v1/auth/logout", headers=old).status_code == 204
    assert (
        client.get(
            f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=old
        ).status_code
        == 401
    )


def test_a_deleted_document_is_unreachable_from_both_organizations_views(worlds) -> None:
    client = worlds["client"]
    a, b = worlds["a"], worlds["b"]
    assert client.delete(
        f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=a.headers
    ).status_code in (200, 204)
    # A's own members get 404 for the deleted document (not 200, not a stale export).
    gone = client.get(
        f"/api/v1/documents/{worlds['doc']}/export?organization_id={a.org}&format=json",
        headers=a.headers,
    )
    assert gone.status_code == 404
    # Organization B's members are refused by the membership check before any lookup.
    other = client.get(
        f"/api/v1/documents/{worlds['doc']}/export?organization_id={b.org}&format=json",
        headers=b.headers,
    )
    assert other.status_code in (403, 404)


def test_a_foreign_id_and_a_missing_id_are_indistinguishable(worlds) -> None:
    """A caller must not learn whether an id exists in another organization: both answers are identical."""
    import uuid as _uuid

    client = worlds["client"]
    b = worlds["b"]
    missing = str(_uuid.uuid4())
    for path in (
        "/api/v1/documents/{}",
        "/api/v1/documents/{}/status",
        "/api/v1/documents/{}/extraction",
        "/api/v1/documents/{}/export?format=json",
    ):
        foreign = client.get(
            path.format(worlds["doc"]) + f"{'&' if '?' in path else '?'}organization_id={b.org}",
            headers=b.headers,
        )
        absent = client.get(
            path.format(missing) + f"{'&' if '?' in path else '?'}organization_id={b.org}",
            headers=b.headers,
        )
        assert foreign.status_code == absent.status_code, path
        assert foreign.json() == absent.json(), path  # same body, so no oracle
        assert (
            worlds["a"].org not in foreign.text
        )  # the other organization's id is never echoed back


def test_review_queue_lists_only_the_requested_organizations_documents(worlds) -> None:
    client = worlds["client"]
    a, b, double = worlds["a"], worlds["b"], worlds["double"]
    queue_a = client.get(
        f"/api/v1/review/queue?organization_id={a.org}", headers=double.headers
    ).json()
    # The mock extraction flags total_amount (confidence 0.55), so A's document is in A's queue...
    assert worlds["doc"] in {item["document_id"] for item in queue_a}
    queue_b = client.get(
        f"/api/v1/review/queue?organization_id={b.org}", headers=double.headers
    ).json()
    assert worlds["doc"] not in {item["document_id"] for item in queue_b}


def test_audit_logs_never_show_another_organizations_events(worlds) -> None:
    client = worlds["client"]
    a, b = worlds["a"], worlds["b"]
    # Sanity: A's own trail does contain its document, so the absence below is meaningful.
    logs_a = client.get(f"/api/v1/audit-logs?organization_id={a.org}", headers=a.headers)
    assert logs_a.status_code == 200 and worlds["doc"] in logs_a.text
    logs_b = client.get(f"/api/v1/audit-logs?organization_id={b.org}", headers=b.headers)
    assert logs_b.status_code == 200
    assert worlds["doc"] not in logs_b.text  # A's document id never appears in B's audit trail
    assert worlds["field"] not in logs_b.text


def test_error_bodies_do_not_echo_the_requested_foreign_id_in_stack_traces(worlds) -> None:
    client = worlds["client"]
    response = client.get(
        f"/api/v1/documents/not-a-uuid?organization_id={worlds['b'].org}",
        headers=worlds["b"].headers,
    )
    assert response.status_code in (404, 422)
    assert "Traceback" not in response.text and 'File "' not in response.text


def test_an_expired_or_forged_token_is_refused_for_every_organization_route(worlds) -> None:
    from datetime import UTC, datetime, timedelta

    import jwt as pyjwt

    from app.core import settings

    client = worlds["client"]
    a, double = worlds["a"], worlds["double"]
    subject = _uid(double)
    expired = pyjwt.encode(
        {"sub": subject, "sv": 1, "exp": datetime.now(UTC) - timedelta(seconds=5)},
        settings().effective_jwt_secret,
        algorithm=settings().jwt_algorithm,
    )
    forged = pyjwt.encode(
        {"sub": subject, "sv": 1, "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "attacker-controlled-secret-of-sufficient-length-123",
        algorithm="HS256",
    )
    for token in (expired, forged):
        headers = {"Authorization": f"Bearer {token}"}
        for org in (a.org, worlds["b"].org):
            response = client.get(
                f"/api/v1/documents/{worlds['doc']}?organization_id={org}", headers=headers
            )
            assert response.status_code == 401, (org, response.status_code)


def test_an_expired_api_key_is_refused_even_though_it_is_not_revoked(worlds) -> None:
    from datetime import UTC, datetime, timedelta

    from app.models import ApiKey

    client = worlds["client"]
    a = worlds["a"]
    key = client.post(
        f"/api/v1/api-keys?organization_id={a.org}&name=short-lived",
        json=["documents:read"],
        headers=a.headers,
    ).json()
    headers = {"Authorization": f"Bearer {key['key']}"}
    assert (
        client.get(
            f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=headers
        ).status_code
        == 200
    )
    with SessionLocal.begin() as db:
        row = db.get(ApiKey, uuid.UUID(key["id"]))
        assert row is not None and row.revoked_at is None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (
        client.get(
            f"/api/v1/documents/{worlds['doc']}?organization_id={a.org}", headers=headers
        ).status_code
        == 401
    )
