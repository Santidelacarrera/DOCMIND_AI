"""S3StorageProvider against an in-process S3 (moto): the same contract as local storage.

Covers what production relies on: round trip, SSE-KMS requested on every write when a key is
configured, a missing object reported as FileNotFoundError (callers map it to a permanent
"not found", not a retry or a 500), other errors propagated, listing across pages, and the
organization prefix enforced by owned_key before any S3 call.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from app import storage as storage_module
from app.core import Settings
from app.storage import S3StorageProvider, StorageAccessError, owned_key

BUCKET = "docmind-test-bucket"
REGION = "us-east-1"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[S3StorageProvider, Any]]:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    with moto.mock_aws():
        kms = boto3.client("kms", region_name=REGION)
        key_id = kms.create_key(Description="docmind test")["KeyMetadata"]["KeyId"]
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        config = Settings(
            _env_file=None, storage_provider="s3", s3_bucket=BUCKET, s3_region=REGION, s3_kms_key_id=key_id,
        )
        monkeypatch.setattr(storage_module, "settings", lambda: config)
        yield S3StorageProvider(), key_id


def test_round_trip_with_kms_encryption(s3) -> None:
    provider, key_id = s3
    org = uuid.uuid4()
    key = f"{org}/doc/file.pdf"
    provider.put(owned_key(org, key), b"%PDF-1.7 test")
    assert provider.get(owned_key(org, key)) == b"%PDF-1.7 test"
    head = provider.client.head_object(Bucket=BUCKET, Key=key)
    assert head["ServerSideEncryption"] == "aws:kms"
    assert head["SSEKMSKeyId"].endswith(key_id)


def test_without_kms_key_no_encryption_header_is_sent(s3, monkeypatch) -> None:
    provider, _ = s3
    provider.kms_key_id = None
    provider.put("org/plain.txt", b"x")
    assert "SSEKMSKeyId" not in provider.client.head_object(Bucket=BUCKET, Key="org/plain.txt")


def test_missing_object_is_file_not_found(s3) -> None:
    provider, _ = s3
    with pytest.raises(FileNotFoundError):
        provider.get("org/never-uploaded.pdf")


def test_other_errors_propagate(s3) -> None:
    from botocore.exceptions import ClientError

    provider, _ = s3
    provider.bucket = "bucket-that-does-not-exist"
    with pytest.raises(ClientError):
        provider.get("org/file.pdf")


def test_delete_and_listing_across_pages(s3) -> None:
    provider, _ = s3
    keys = [f"org/{i:04d}.bin" for i in range(1005)]  # more than one list_objects_v2 page
    for key in keys:
        provider.client.put_object(Bucket=BUCKET, Key=key, Body=b"1")
    listed = [key for key, modified in provider.list_objects()]
    assert sorted(listed) == keys
    provider.delete(keys[0])
    provider.delete("org/absent.bin")  # deleting what is not there is not an error
    assert keys[0] not in [key for key, _ in provider.list_objects()]


def test_keys_outside_the_organization_prefix_are_refused_before_s3(s3) -> None:
    provider, _ = s3
    org, other = uuid.uuid4(), uuid.uuid4()
    provider.put(f"{other}/secret.pdf", b"other tenant")
    with pytest.raises(StorageAccessError):
        provider.get(owned_key(org, f"{other}/secret.pdf"))
