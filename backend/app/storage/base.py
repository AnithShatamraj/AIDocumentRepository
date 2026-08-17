"""Storage backend interface. Binaries live here; Postgres stores keys only."""
from __future__ import annotations

import abc


class StorageBackend(abc.ABC):
    @abc.abstractmethod
    def ensure_container(self) -> None:
        """Create this backend's bucket/container if it doesn't exist yet.
        Called during tenant provisioning; safe to call again (no-op if present)."""
        ...

    @abc.abstractmethod
    def put_object(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        ...

    @abc.abstractmethod
    def get_bytes(self, key: str) -> bytes:
        ...

    @abc.abstractmethod
    def presigned_get_url(self, key: str, expires_seconds: int = 900, filename: str | None = None) -> str:
        """Short-lived download URL issued only after a permission check."""
        ...

    @abc.abstractmethod
    def delete_object(self, key: str) -> None:
        ...
