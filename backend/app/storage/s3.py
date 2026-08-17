"""S3-compatible backend (AWS S3 and MinIO for local dev)."""
from __future__ import annotations

import boto3
from botocore.client import Config

from app.core.config import settings
from app.storage.base import StorageBackend


class S3Storage(StorageBackend):
    def __init__(self, bucket: str) -> None:
        def make_client(endpoint: str | None):
            kwargs = dict(
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
                config=Config(signature_version="s3v4"),
            )
            # MinIO / custom endpoint; omit for real AWS to use default resolution.
            if endpoint:
                kwargs["endpoint_url"] = endpoint
            return boto3.client("s3", **kwargs)

        internal = settings.s3_endpoint_url if (
            settings.storage_provider == "minio" or settings.s3_endpoint_url) else None
        self._client = make_client(internal)
        # Presigned URLs embed the signing host, so they must be generated
        # against a browser-resolvable endpoint (e.g. localhost:9000, not the
        # docker-internal minio:9000).
        public = settings.s3_public_endpoint_url or internal
        self._public_client = self._client if public == internal else make_client(public)
        self._bucket = bucket

    def ensure_container(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            self._client.create_bucket(Bucket=self._bucket)

    def put_object(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def get_bytes(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()

    def presigned_get_url(self, key: str, expires_seconds: int = 900, filename: str | None = None) -> str:
        params = {"Bucket": self._bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self._public_client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires_seconds
        )

    def delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)
