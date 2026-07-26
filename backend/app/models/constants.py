"""String constants used across models (kept as strings, not PG enums, so the
taxonomy can evolve as data without ALTER TYPE migrations)."""
from __future__ import annotations

# --- Roles (application capabilities) ---
ROLE_ADMIN = "administrator"
ROLE_REVIEWER = "reviewer"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_REVIEWER, ROLE_VIEWER)

# --- Permission levels (data access on a document) ---
PERM_READ = "read"
PERM_UPDATE = "update"
PERM_DELETE = "delete"
PERM_MANAGE = "manage"  # right to grant/revoke access
PERM_LEVELS = (PERM_READ, PERM_UPDATE, PERM_DELETE, PERM_MANAGE)

# --- Document processing status (per-document rollup) ---
DOC_PENDING = "pending"
DOC_PROCESSING = "processing"
DOC_PROCESSED = "processed"
DOC_FAILED = "failed"
DOC_NEEDS_REVIEW = "needs_review"

# --- Pipeline stages (order matters: it's the Celery chain) ---
STAGE_EXTRACT = "text_extraction"
STAGE_UNDERSTAND = "document_understanding"
STAGE_CLASSIFY = "classification"
STAGE_SUMMARIZE = "summarization"
STAGE_METADATA = "metadata_extraction"
STAGE_CHUNK = "chunking"
STAGE_EMBED = "embedding"
PIPELINE_STAGES = (
    STAGE_EXTRACT,
    STAGE_UNDERSTAND,
    STAGE_CLASSIFY,
    STAGE_SUMMARIZE,
    STAGE_METADATA,
    STAGE_CHUNK,
    STAGE_EMBED,
)

# --- Stage / run state ---
ST_PENDING = "pending"
ST_RUNNING = "running"
ST_SUCCEEDED = "succeeded"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"

# --- Review status for extractions & review items ---
RV_AUTO_ACCEPTED = "auto_accepted"
RV_PENDING = "pending_review"
RV_VERIFIED = "verified"
RV_CORRECTED = "corrected"
RV_REJECTED = "rejected"

# --- Review item kinds ---
REVIEW_EXTRACTION = "extraction"
REVIEW_CLASSIFICATION = "classification"

# --- Normalized value types for extracted metadata ---
VT_TEXT = "text"
VT_DATE = "date"
VT_NUMERIC = "numeric"
VT_CURRENCY = "currency"

# --- Notification kinds ---
NOTIFY_PROCESSING_DONE = "processing_complete"
NOTIFY_PROCESSING_FAILED = "processing_failed"
NOTIFY_REVIEW_PENDING = "review_pending"

# --- Audit event types ---
AUDIT_UPLOAD = "document.upload"
AUDIT_DOWNLOAD = "document.download"
AUDIT_VIEW = "document.view"
AUDIT_DELETE = "document.delete"
AUDIT_RESTORE = "document.restore"
AUDIT_PERM_CHANGE = "permission.change"
AUDIT_METADATA_CHANGE = "metadata.change"
AUDIT_REVIEW = "review.activity"
AUDIT_LOGIN = "auth.login"
AUDIT_CONFIG_CHANGE = "admin.config_change"
