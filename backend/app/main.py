"""FastAPI application entrypoint."""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, correlation_id, get_logger

configure_logging(settings.log_level)
log = get_logger("api")

# One line per tag, shown as the section header in Swagger UI / ReDoc.
TAGS_METADATA = [
    {"name": "auth", "description": "Login and session identity."},
    {"name": "users", "description": "User and group management (tenant administrators)."},
    {"name": "document-types", "description": "Document type definitions and versioned field schemas."},
    {"name": "documents", "description": "Upload, retrieval, versioning, extracted-data review, and lifecycle."},
    {"name": "permissions", "description": "Per-document sharing -- grant/revoke access."},
    {"name": "pipeline", "description": "Processing pipeline status and live progress (Server-Sent Events)."},
    {"name": "search", "description": "Semantic (vector/keyword/hybrid) and structured field search."},
    {"name": "chat", "description": "AI Research Assistant -- streaming RAG chat over your documents."},
    {"name": "review", "description": "Cross-document review queue for low-confidence AI results."},
    {"name": "dashboard", "description": "Operational metrics and widgets."},
    {"name": "admin", "description": "Tenant configuration: AI provider overrides, confidence thresholds, prompt versions."},
    {"name": "notifications", "description": "In-app notifications."},
    {"name": "audit", "description": "Audit log of tenant activity."},
]

# Paths that don't require (or don't use header-based) bearer auth -- excluded
# from the blanket "security" requirement added below so Swagger UI doesn't
# imply a padlock where none applies.
_PUBLIC_OR_QUERY_AUTH_PATHS = {"/health", "/api/auth/login"}


app = FastAPI(
    title="AI Document Repository",
    version="0.1.0",
    description="AI-first document management: ingestion pipeline, semantic search, grounded Q&A.",
    openapi_tags=TAGS_METADATA,
)


def custom_openapi():
    """Inject a Bearer security scheme into the generated OpenAPI schema.

    Every route already enforces the JWT itself (see `app/api/deps.py`) by
    reading the raw `Authorization` header -- there is no FastAPI
    `HTTPBearer`/`OAuth2` dependency for auto-generation to pick up. Without
    this, Swagger UI has no "Authorize" button and an OpenAPI import into
    Postman has no security scheme to attach. This only edits the generated
    schema (adds `components.securitySchemes` + a `security` requirement per
    operation); it changes no request-handling behavior.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description,
                         routes=app.routes, tags=TAGS_METADATA)
    schema.setdefault("components", {})["securitySchemes"] = {
        "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    }
    for path, methods in schema.get("paths", {}).items():
        if path in _PUBLIC_OR_QUERY_AUTH_PATHS or path.endswith("/events"):
            continue
        for operation in methods.values():
            if isinstance(operation, dict):
                operation.setdefault("security", [{"bearerAuth": []}])
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    cid = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    correlation_id.set(cid)
    response = await call_next(request)
    response.headers["x-correlation-id"] = cid
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health():
    return {"status": "ok", "provider": settings.effective_ai_provider,
            "embedding_provider": settings.effective_embedding_provider}


app.include_router(api_router)
