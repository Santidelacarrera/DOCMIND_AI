"""Conversation turns sealed per organization (design from ContextRAG_Engine ``memory/store.py``).

* Key: HKDF-SHA256 of ``CONVERSATION_ENCRYPTION_KEY`` with the organization id as ``info``, so one
  organization's key never opens another's turns, and the master secret never touches a row.
* Cipher: AES-256-GCM, a fresh 96-bit nonce per turn, stored as ``nonce || ciphertext``.
* Position binding: the associated data is ``conversation:seq:role``. A row copied to another
  conversation, position or role fails authentication instead of being shown out of context.
* Reads fail closed with :class:`ConversationIntegrityError`: wrong key, other organization, tampering.
  There is no plaintext fallback.
* Turns are immutable and numbered 1, 2, ... per conversation (unique ``(conversation_id, seq)``): two
  concurrent appends cannot interleave silently; the loser gets a conflict and retries.
"""

import json
import os
import uuid
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core import settings

NONCE = 12


class ConversationsDisabled(RuntimeError):
    code = "CONVERSATIONS_DISABLED"


class ConversationIntegrityError(RuntimeError):
    code = "CONVERSATION_INTEGRITY"


def _key(organization_id: uuid.UUID) -> bytes:
    secret = settings().conversation_encryption_key
    if secret is None or not secret.get_secret_value():
        raise ConversationsDisabled()
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"docmind-conversations-v1",
        info=str(organization_id).encode(),
    ).derive(secret.get_secret_value().encode())


def enabled() -> bool:
    secret = settings().conversation_encryption_key
    return secret is not None and bool(secret.get_secret_value())


def _aad(conversation_id: uuid.UUID, seq: int, role: str) -> bytes:
    return f"{conversation_id}:{seq}:{role}".encode()


def seal(organization_id: uuid.UUID, conversation_id: uuid.UUID, seq: int, role: str, content: dict[str, Any]) -> bytes:
    nonce = os.urandom(NONCE)
    body = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    return nonce + AESGCM(_key(organization_id)).encrypt(nonce, body, _aad(conversation_id, seq, role))


def open_turn(organization_id: uuid.UUID, conversation_id: uuid.UUID, seq: int, role: str, payload: bytes) -> dict[str, Any]:
    if len(payload) <= NONCE:
        raise ConversationIntegrityError()
    try:
        body = AESGCM(_key(organization_id)).decrypt(payload[:NONCE], payload[NONCE:], _aad(conversation_id, seq, role))
        value = json.loads(body)
    except (InvalidTag, ValueError):
        raise ConversationIntegrityError() from None
    if not isinstance(value, dict):
        raise ConversationIntegrityError()
    return value
