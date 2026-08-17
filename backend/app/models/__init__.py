"""Import all models so SQLAlchemy metadata is fully populated."""
from app.models.base import TimestampMixin, uuid_pk  # noqa: F401
from app.models.tenant import Group, User, user_group  # noqa: F401
from app.models.catalog import DocumentType, TypeSchema  # noqa: F401
from app.models.document import (  # noqa: F401
    Document,
    DocumentPermission,
    DocumentTypeDefaultPermission,
    DocumentVersion,
    document_type_links,
)
from app.models.pipeline import PipelineRun, PipelineStage  # noqa: F401
from app.models.content import (  # noqa: F401
    Chunk,
    Classification,
    Embedding,
    FieldValue,
    Summary,
)
from app.models.review import ReviewItem  # noqa: F401
from app.models.chat import Conversation, Message, QAAuditLog  # noqa: F401
from app.models.ops import (  # noqa: F401
    AIConfig,
    AuditLog,
    CostRecord,
    Notification,
    PromptVersion,
)

__all__ = [
    "User",
    "Group",
    "user_group",
    "DocumentType",
    "TypeSchema",
    "Document",
    "DocumentVersion",
    "DocumentPermission",
    "DocumentTypeDefaultPermission",
    "document_type_links",
    "PipelineRun",
    "PipelineStage",
    "Chunk",
    "Embedding",
    "Summary",
    "Classification",
    "FieldValue",
    "ReviewItem",
    "Conversation",
    "Message",
    "QAAuditLog",
    "AuditLog",
    "Notification",
    "PromptVersion",
    "AIConfig",
    "CostRecord",
]
