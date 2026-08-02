"""Pydantic request/response models."""
from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, EmailStr, Field


# ------------------------------------------------------------------- auth
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: str
    tenant_id: uuid.UUID

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = ""
    password: str = Field(min_length=8)
    role: str = "viewer"


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str

    class Config:
        from_attributes = True


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    member_ids: list[uuid.UUID] = []


# ------------------------------------------------------------------- document types
class DocumentTypeOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    is_enabled: bool
    active_schema_id: uuid.UUID | None = None

    class Config:
        from_attributes = True


class DocumentTypeCreate(BaseModel):
    name: str
    description: str = ""
    fields: list[dict] | None = None  # optional extraction schema fields


class DocumentTypeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_enabled: bool | None = None
    fields: list[dict] | None = None


# ------------------------------------------------------------------- documents
class DocumentOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    file_type: str
    mime_type: str
    file_size: int
    content_hash: str
    current_version: int
    processing_status: str
    owner_id: uuid.UUID
    created_at: dt.datetime

    class Config:
        from_attributes = True


class DocumentDetail(DocumentOut):
    document_types: list[DocumentTypeOut] = []
    summary: dict | None = None
    fields: list[dict] = []
    extractions: list[dict] = []  # deprecated flat alias
    classifications: list[dict] = []


class PresignedUrl(BaseModel):
    url: str
    expires_in: int


# ------------------------------------------------------------------- permissions
class PermissionGrant(BaseModel):
    user_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    level: str  # read | update | delete | manage


class PermissionOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    group_id: uuid.UUID | None
    level: str

    class Config:
        from_attributes = True


# ------------------------------------------------------------------- search / chat
class SearchRequest(BaseModel):
    query: str
    mode: str = "hybrid"  # vector | keyword | hybrid
    k: int = 10


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    content: str
    page: int | None
    anchor: str | None
    section_title: str | None
    score: float


class StructuredSearchRequest(BaseModel):
    field_name: str | None = None
    op: str = "eq"
    value: str | None = None
    value2: str | None = None
    document_type_id: uuid.UUID | None = None
    verified_only: bool = False
    limit: int = 100


class ChatRequest(BaseModel):
    question: str
    conversation_id: uuid.UUID | None = None


# ------------------------------------------------------------------- review
class ReviewOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    kind: str
    field_name: str | None
    confidence: float
    status: str
    claimed_by: uuid.UUID | None
    payload: dict | None
    created_at: dt.datetime

    class Config:
        from_attributes = True


class ReviewResolve(BaseModel):
    action: str  # accept | modify | reject
    corrected_value: str | None = None
    document_type_ids: list[uuid.UUID] | None = None  # for classification review
    notes: str | None = None


class ExtractionReview(BaseModel):
    """Inline accept/reject of an extracted field from the document viewer."""

    action: str  # accept | reject
    notes: str | None = None


# ------------------------------------------------------------------- admin
class ThresholdUpdate(BaseModel):
    confidence_thresholds: dict[str, float] | None = None
    provider_overrides: dict | None = None
    processing_config: dict | None = None


class PromptVersionCreate(BaseModel):
    key: str
    template: str
