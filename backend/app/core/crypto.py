"""Encryption for secrets stored at rest in the management database (tenant DB
passwords). App-level symmetric encryption via Fernet -- not a full secrets-
manager integration, but a proportionate mitigation against the management
database itself being a skeleton key: a leak of that database alone doesn't
hand over every tenant's plaintext database password.

Swap-in point for Key Vault / AWS KMS / Vault later: every caller only ever
uses encrypt_secret/decrypt_secret, so hardening later only means changing
_fernet()'s key source here.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import settings


@lru_cache
def _fernet() -> Fernet:
    key = settings.tenant_db_encryption_key
    if not key:
        # Dev convenience: derive a stable key from secret_key so the stack
        # boots with zero extra configuration, matching this app's existing
        # "zero-config locally, production overrides everything" philosophy.
        # Production must set TENANT_DB_ENCRYPTION_KEY explicitly.
        key = base64.urlsafe_b64encode(
            hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
        ).decode("utf-8")
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
