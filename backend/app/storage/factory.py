"""Storage backend factory — selects implementation from settings."""
from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.storage.base import StorageBackend


@lru_cache
def get_storage() -> StorageBackend:
    provider = settings.storage_provider
    if provider in ("s3", "minio"):
        from app.storage.s3 import S3Storage

        return S3Storage()
    if provider == "azure":
        from app.storage.azure import AzureBlobStorage

        return AzureBlobStorage()
    if provider == "gcs":
        raise NotImplementedError(
            "GCS backend not yet implemented; add app/storage/gcs.py using google-cloud-storage."
        )
    raise ValueError(f"Unknown storage provider: {provider}")
