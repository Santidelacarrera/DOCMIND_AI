"""Email invitations and outbound webhooks."""

import re

from app.notifications import ConsoleEmailProvider
from app.webhooks import WebhookURLError, sign_payload, signing_secret, validate_url


def _invite(client, actor, email, role="MEMBER", organization_id=None):
    return client.post(
        f"/api/v1/invitations?organization_id={organization_id or actor.org}",
        json={"email": email, "role": role},
        headers=actor.headers,
    )


def test_invite_sends_email_and_lists_as_pending(client, register) -> None:
    owner = register("owner")
    response = _invite(client, owner, "new-hire@example.com", "ADMIN")
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new-hire@example.com" and body["status"] == "PENDING"
    pending = client.get(f"/api/v1/invitations?organization_id={owner.org}", headers=owner.headers).json()
    assert pending[0]["email"] == "new-hire@example.com"
    # The console provider is selected whenever SMTP_HOST is unset (tests/dev).
    assert ConsoleEmailProvider.sent
    to, _subject, body_text = ConsoleEmailProvider.sent[-1]
    assert to == "new-hire@example.com"
    assert "invitations/accept?token=" in body_text


def test_duplicate_invitation_and_existing_member_are_rejected(client, register) -> None:
    owner, member = register("owner"), register("member")
    client.post(
        f"/api/v1/members?organization_id={owner.org}&email={member.email}", headers=owner.headers
    )
    assert _invite(client, owner, member.email).status_code == 409
    assert _invite(client, owner, "pending@example.com").status_code == 201
    assert _invite(client, owner, "pending@example.com").status_code == 409


def test_non_admin_cannot_invite(client, register) -> None:
    owner, viewer = register("owner"), register("viewer")
    client.post(
        f"/api/v1/members?organization_id={owner.org}&email={viewer.email}&role=VIEWER",
        headers=owner.headers,
    )
    assert (
        _invite(client, viewer, "x@example.com", organization_id=owner.org).status_code == 403
    )


def test_accept_invitation_requires_matching_email_and_grants_membership(client, register) -> None:
    owner = register("owner")
    _invite(client, owner, "invitee@example.com", "ADMIN")
    _to, _subject, body_text = ConsoleEmailProvider.sent[-1]
    token = re.search(r"token=([\w-]+)", body_text).group(1)

    wrong_person = register("wrong")
    mismatched = client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=wrong_person.headers
    )
    assert mismatched.status_code == 403

    # Register the invitee with the exact invited address (the invitation is
    # only usable by that address).
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "invitee@example.com",
            "password": "correct-horse-battery-staple",
            "organization_name": "invitee-personal-space",
        },
    )
    assert registered.status_code == 201
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    accepted = client.post("/api/v1/invitations/accept", json={"token": token}, headers=headers)
    assert accepted.status_code == 200
    assert accepted.json()["organization_id"] == owner.org
    assert accepted.json()["role"] == "ADMIN"

    # Re-using the token fails; it is no longer pending.
    assert client.post("/api/v1/invitations/accept", json={"token": token}, headers=headers).status_code == 404


def test_revoke_invitation(client, register) -> None:
    owner = register("owner")
    invite_id = _invite(client, owner, "revoke-me@example.com").json()["id"]
    assert (
        client.delete(
            f"/api/v1/invitations/{invite_id}?organization_id={owner.org}", headers=owner.headers
        ).status_code
        == 204
    )
    listed = client.get(f"/api/v1/invitations?organization_id={owner.org}", headers=owner.headers).json()
    assert next(i for i in listed if i["id"] == invite_id)["status"] == "REVOKED"


def test_webhook_url_validation_rejects_private_and_non_https() -> None:
    for bad in ("http://example.com/hook", "https://localhost/hook", "https://127.0.0.1/hook", "https://169.254.169.254/hook"):
        try:
            validate_url(bad)
            raise AssertionError(f"expected rejection for {bad}")
        except WebhookURLError:
            pass
    validate_url("https://example.com/hook")  # public hostname: accepted


def test_webhook_signature_is_deterministic_hmac() -> None:
    import uuid

    webhook_id = uuid.uuid4()
    body = b'{"event": "document.completed"}'
    assert sign_payload(webhook_id, body) == sign_payload(webhook_id, body)
    assert len(signing_secret(webhook_id)) == 64  # hex sha256


def test_webhook_crud_and_dispatch_on_completion(client, register) -> None:
    from app import worker

    acct = register()
    created = client.post(
        f"/api/v1/webhooks?organization_id={acct.org}",
        json={"url": "https://example.com/hooks/docmind", "events": ["document.completed"]},
        headers=acct.headers,
    )
    assert created.status_code == 201
    assert len(created.json()["signing_secret"]) == 64

    listed = client.get(f"/api/v1/webhooks?organization_id={acct.org}", headers=acct.headers).json()
    assert listed[0]["url"] == "https://example.com/hooks/docmind"
    assert "signing_secret" not in listed[0]

    project = acct.project()
    body = acct.upload(project).json()
    worker.process_document.run(body["job_id"])

    assert any(event == "document.completed" for _org, event, _payload in worker.dispatched_webhooks)

    webhook_id = created.json()["id"]
    assert (
        client.delete(
            f"/api/v1/webhooks/{webhook_id}?organization_id={acct.org}", headers=acct.headers
        ).status_code
        == 204
    )


def test_create_webhook_rejects_unsafe_url(client, register) -> None:
    acct = register()
    response = client.post(
        f"/api/v1/webhooks?organization_id={acct.org}",
        json={"url": "https://localhost/hook", "events": ["document.completed"]},
        headers=acct.headers,
    )
    assert response.status_code == 422
