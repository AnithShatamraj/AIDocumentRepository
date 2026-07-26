"""FastAPI application entrypoint."""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, correlation_id, get_logger

configure_logging(settings.log_level)
log = get_logger("api")

app = FastAPI(
    title="AI Document Repository",
    version="0.1.0",
    description="AI-first document management: ingestion pipeline, semantic search, grounded Q&A.",
)

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
