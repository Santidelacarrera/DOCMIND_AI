from pathlib import Path
from typing import Protocol

from app.core import settings


class StorageAccessError(Exception):
    """A storage key does not belong to the organization asking for it."""


def owned_key(organization_id: object, key: str) -> str:
    """Return ``key`` only if it lives under ``organization_id``'s prefix.

    Keys are generated as ``<organization_id>/<uuid>.pdf``. The API already scopes every
    query by organization; this is the independent second check at the storage boundary,
    so a tampered/misrouted database row can never make one tenant read or delete
    another tenant's object.
    """
    prefix = f"{organization_id}/"
    if not key.startswith(prefix) or ".." in key.split("/") or "\\" in key:
        raise StorageAccessError("STORAGE_KEY_NOT_OWNED")
    return key


class StorageProvider(Protocol):
    def put(self, key: str, content: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalStorageProvider:
    def __init__(self) -> None:
        self.root = settings().local_storage_path.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("invalid storage key")
        return path

    def put(self, key: str, content: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()


class S3StorageProvider:
    """Private S3-compatible object storage; credentials come from workload identity/env."""

    def __init__(self) -> None:
        import boto3

        config = settings()
        self.bucket = config.s3_bucket
        self.kms_key_id = config.s3_kms_key_id
        self.client = boto3.client("s3", endpoint_url=config.s3_endpoint_url, region_name=config.s3_region)

    def put(self, key: str, content: bytes) -> None:
        params = {"Bucket": self.bucket, "Key": key, "Body": content}
        if self.kms_key_id:
            params.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=self.kms_key_id)
        self.client.put_object(**params)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def build_storage() -> StorageProvider:
    if settings().storage_provider == "s3":
        return S3StorageProvider()
    return LocalStorageProvider()


storage: StorageProvider = build_storage()

