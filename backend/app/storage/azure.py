"""Azure Blob Storage backend (preferred cloud target)."""
from __future__ import annotations

import datetime as dt

from app.core.config import settings
from app.storage.base import StorageBackend


class AzureBlobStorage(StorageBackend):
    def __init__(self, container: str) -> None:
        from azure.storage.blob import BlobServiceClient

        if settings.azure_storage_connection_string:
            self._svc = BlobServiceClient.from_connection_string(
                settings.azure_storage_connection_string
            )
        elif settings.azure_storage_account_url:
            # Managed identity / default credential in AKS.
            from azure.identity import DefaultAzureCredential  # type: ignore

            self._svc = BlobServiceClient(
                account_url=settings.azure_storage_account_url,
                credential=DefaultAzureCredential(),
            )
        else:
            raise RuntimeError("Azure storage requires connection string or account URL")
        self._container = container

    def ensure_container(self) -> None:
        from azure.core.exceptions import ResourceExistsError

        try:
            self._svc.create_container(self._container)
        except ResourceExistsError:
            pass

    def _blob(self, key: str):
        return self._svc.get_blob_client(container=self._container, blob=key)

    def put_object(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        from azure.storage.blob import ContentSettings

        self._blob(key).upload_blob(
            data, overwrite=True, content_settings=ContentSettings(content_type=content_type)
        )

    def get_bytes(self, key: str) -> bytes:
        return self._blob(key).download_blob().readall()

    def presigned_get_url(self, key: str, expires_seconds: int = 900, filename: str | None = None) -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        # SAS generation needs an account key; for MI-based auth use user-delegation SAS.
        if settings.azure_storage_connection_string:
            account_key = self._svc.credential.account_key  # type: ignore[attr-defined]
            sas = generate_blob_sas(
                account_name=self._svc.account_name,
                container_name=self._container,
                blob_name=key,
                account_key=account_key,
                permission=BlobSasPermissions(read=True),
                expiry=dt.datetime.utcnow() + dt.timedelta(seconds=expires_seconds),
            )
            return f"{self._blob(key).url}?{sas}"
        # Fallback: return the blob URL (assumes network-restricted or public-read container).
        return self._blob(key).url

    def delete_object(self, key: str) -> None:
        self._blob(key).delete_blob()
