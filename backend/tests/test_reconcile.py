"""Orphaned storage objects: reported by default, deleted only when opted in and only when safe."""

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app import reconcile
from app.core import settings
from app.storage import storage
from tests.conftest import make_pdf


def _put(org: str, name: str | None = None, age_hours: float = 48) -> str:
    key = f"{org}/{name or uuid.uuid4()}.pdf"
    storage.put(key, b"%PDF-1.4 orphan")
    path = storage._path(key)  # type: ignore[attr-defined]
    old = time.time() - age_hours * 3600
    os.utime(path, (old, old))
    return key


def _exists(key: str) -> bool:
    return key in {k for k, _ in storage.list_objects()}


@pytest.fixture(autouse=True)
def _empty_storage():
    """Other tests leave objects behind (the database is reset per test, storage is not)."""
    for key, _ in list(storage.list_objects()):
        storage.delete(key)


@pytest.fixture
def tenant(register):
    return register().org


def test_report_only_by_default(register, tenant) -> None:
    orphan = _put(tenant)
    summary = reconcile.reconcile()
    assert summary["orphans"] == 1 and summary["deleted"] == 0
    assert _exists(orphan)  # nothing is removed unless deletion is enabled


def test_live_documents_and_versions_are_never_orphans(register, client) -> None:
    acct = register()
    upload = acct.upload(acct.project()).json()
    assert upload["document_id"]
    summary = reconcile.reconcile(delete=True, now=datetime.now(UTC) + timedelta(days=30))
    assert summary["orphans"] == 0 and summary["deleted"] == 0
    assert summary["objects"] == 1


def test_delete_removes_only_old_orphans(register, tenant) -> None:
    acct = register()
    live = acct.upload(acct.project()).json()
    assert live["document_id"]
    old = _put(tenant)
    young = _put(tenant, age_hours=1)
    # Two orphans out of three objects would trip the fraction guard; relax it for this test.
    settings().storage_reconcile_max_delete_fraction = 1.0
    try:
        summary = reconcile.reconcile(delete=True)
    finally:
        settings().storage_reconcile_max_delete_fraction = 0.5
    assert (summary["orphans"], summary["deleted"]) == (1, 1)
    assert not _exists(old)
    assert _exists(young)  # inside the grace period: may be an upload still committing


def test_refuses_mass_deletion(register, tenant) -> None:
    orphans = [_put(tenant) for _ in range(3)]
    summary = reconcile.reconcile(
        delete=True
    )  # 3 of 3 objects are orphans: looks like a wrong database
    assert summary["refused"] == "TOO_MANY_ORPHANS" and summary["deleted"] == 0
    assert all(_exists(k) for k in orphans)


def test_unrecognised_keys_are_reported_never_deleted(tenant) -> None:
    storage.put("not-a-tenant/notes.txt", b"x")
    old = time.time() - 72 * 3600
    os.utime(storage._path("not-a-tenant/notes.txt"), (old, old))  # type: ignore[attr-defined]
    settings().storage_reconcile_max_delete_fraction = 1.0
    try:
        summary = reconcile.reconcile(delete=True)
    finally:
        settings().storage_reconcile_max_delete_fraction = 0.5
    assert summary["unrecognised"] == 1 and summary["deleted"] == 0
    assert _exists("not-a-tenant/notes.txt")


def test_rows_without_object_are_counted(register) -> None:
    acct = register()
    acct.upload(acct.project())
    for key, _ in list(storage.list_objects()):
        storage.delete(key)
    assert reconcile.reconcile()["rows_without_object"] == 1


def test_dry_run_deletes_nothing_even_when_deletion_is_enabled(register, tenant) -> None:
    """The simulation lists what would happen and touches nothing, whatever the configuration says."""
    orphans = [_put(tenant) for _ in range(1)]
    _put(tenant, age_hours=1)  # inside the grace period
    settings().storage_reconcile_max_delete_fraction = 1.0
    decisions: list[dict[str, str]] = []
    try:
        summary = reconcile.reconcile(delete=True, dry_run=True, decisions_out=decisions)
    finally:
        settings().storage_reconcile_max_delete_fraction = 0.5
    assert summary["mode"] == "dry_run" and summary["would_delete"] == 1 and summary["deleted"] == 0
    assert all(_exists(k) for k in orphans)
    actions = {d["action"] for d in decisions}
    assert actions == {"would_delete", "keep"}
    assert {d["reason"] for d in decisions} == {"orphan", "within_grace_period"}


def test_decisions_name_objects_by_fingerprint_not_by_key(register, tenant) -> None:
    key = _put(tenant)
    decisions: list[dict[str, str]] = []
    reconcile.reconcile(decisions_out=decisions)
    assert decisions and all(tenant not in d["object"] for d in decisions)
    assert key.split("/")[0] not in str(
        decisions
    )  # the tenant identifier never appears in the report


def test_default_configuration_is_report_only(register, tenant) -> None:
    key = _put(tenant)
    summary = reconcile.reconcile()
    assert summary["mode"] == "report" and summary["deleted"] == 0 and _exists(key)
    assert settings().storage_reconcile_delete is False  # the shipped default


def test_a_storage_listing_failure_deletes_nothing(register, tenant, monkeypatch) -> None:
    orphan = _put(tenant)
    settings().storage_reconcile_delete = True

    def broken_listing():
        raise ConnectionError("storage unreachable")
        yield  # pragma: no cover

    monkeypatch.setattr(storage, "list_objects", broken_listing)
    try:
        with pytest.raises(ConnectionError):
            reconcile.reconcile(delete=True)
    finally:
        settings().storage_reconcile_delete = False
    assert (
        storage._path(orphan).exists()
    )  # checked on disk: the listing itself is the broken part  # type: ignore[attr-defined]


def test_one_failed_deletion_does_not_stop_the_others(register, tenant, monkeypatch) -> None:
    keys = [_put(tenant) for _ in range(3)]
    _put(tenant, age_hours=1)  # keeps the orphans below the safety fraction
    settings().storage_reconcile_max_delete_fraction = 1.0
    settings().storage_reconcile_delete = True
    real_delete = storage.delete
    failing = keys[1]

    def flaky_delete(key: str) -> None:
        if key == failing:
            raise OSError("transient storage error")
        real_delete(key)

    monkeypatch.setattr(storage, "delete", flaky_delete)
    decisions: list[dict[str, str]] = []
    try:
        summary = reconcile.reconcile(delete=True, decisions_out=decisions)
    finally:
        settings().storage_reconcile_max_delete_fraction = 0.5
        settings().storage_reconcile_delete = False
    assert (summary["deleted"], summary["orphans"]) == (2, 3)
    assert _exists(failing) and not _exists(keys[0]) and not _exists(keys[2])
    assert sorted(d["reason"] for d in decisions if d["reason"] in ("orphan", "delete_failed")) == [
        "delete_failed",
        "orphan",
        "orphan",
    ]


def test_a_deleted_document_is_not_reported_as_a_missing_object(register, client, tenant) -> None:
    """Its object was removed on purpose at deletion; the row stays until the purge. Not an inconsistency."""
    acct = register()
    upload = acct.upload(acct.project(), make_pdf(1)).json()
    assert client.delete(
        f"/api/v1/documents/{upload['document_id']}?organization_id={acct.org}",
        headers=acct.headers,
    ).status_code in (200, 204)
    summary = reconcile.reconcile(dry_run=True)
    assert summary["rows_without_object"] == 0


def test_a_live_document_whose_object_is_missing_is_reported(register, client) -> None:
    acct = register()
    upload = acct.upload(acct.project(), make_pdf(1)).json()
    for key, _ in list(storage.list_objects()):
        storage.delete(key)  # the object vanished while the document is live
    summary = reconcile.reconcile(dry_run=True)
    assert summary["rows_without_object"] == 1
    assert upload["document_id"]
