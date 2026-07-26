"""Import all models so SQLAlchemy metadata is fully populated."""
from app.models.base import TimestampMixin, uuid_pk  # noqa: F401
from app.models.tenant import Group, Tenant, User, user_group  # noqa: F401
from app.models.catalog import Category, ExtractionSchema  # noqa: F401
from app.models.document import (  # noqa: F401
    CategoryDefaultPermission,
    Document,
    DocumentPermission,
    DocumentVersion,
    document_category,
)
from app.models.pipeline import PipelineRun, PipelineStage  # noqa: F401
from app.models.content import (  # noqa: F401
    Chunk,
    Classification,
    Embedding,
    Extraction,
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
    "Tenant",
    "User",
    "Group",
    "user_group",
    "Category",
    "ExtractionSchema",
    "Document",
    "DocumentVersion",
    "DocumentPermission",
    "CategoryDefaultPermission",
    "document_category",
    "PipelineRun",
    "PipelineStage",
    "Chunk",
    "Embedding",
    "Summary",
    "Classification",
    "Extraction",
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
