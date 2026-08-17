"""Storage backend factory -- selects implementation from a tenant's own
storage_provider/storage_container, so each tenant reads/writes its own
dedicated bucket/container.

Cached per (provider, bucket) rather than the old zero-arg @lru_cache
singleton: one process now serves every tenant, not just one bucket for its
whole lifetime.
"""
from __future__ import annotations

from typing import Protocol

from app.storage.base import StorageBackend

_storage_cache: dict[tuple[str, str], StorageBackend] = {}


class _TenantStorageInfo(Protocol):
    storage_provider: str
    storage_container: str


def get_storage(tenant: _TenantStorageInfo) -> StorageBackend:
    """tenant: any object exposing .storage_provider / .storage_container
    (a management-DB Tenant record, or an equivalent stub -- see
    app/cli.py::provision_tenant, which resolves a bucket/container before
    the Tenant row itself is committed)."""
    provider = tenant.storage_provider
    bucket = tenant.storage_container
    key = (provider, bucket)
    if key not in _storage_cache:
        if provider in ("s3", "minio"):
            from app.storage.s3 import S3Storage

            _storage_cache[key] = S3Storage(bucket=bucket)
        elif provider == "azure":
            from app.storage.azure import AzureBlobStorage

            _storage_cache[key] = AzureBlobStorage(container=bucket)
        elif provider == "gcs":
            raise NotImplementedError(
                "GCS backend not yet implemented; add app/storage/gcs.py using google-cloud-storage."
            )
        else:
            raise ValueError(f"Unknown storage provider: {provider}")
    return _storage_cache[key]
