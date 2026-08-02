"""Aggregate API router."""
from fastapi import APIRouter

from app.api.routers import (
    admin,
    audit,
    auth,
    document_types,
    chat,
    dashboard,
    documents,
    notifications,
    permissions,
    pipeline,
    review,
    search,
    users,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(document_types.router)
api_router.include_router(documents.router)
api_router.include_router(permissions.router)
api_router.include_router(pipeline.router)
api_router.include_router(search.router)
api_router.include_router(chat.router)
api_router.include_router(review.router)
api_router.include_router(dashboard.router)
api_router.include_router(admin.router)
api_router.include_router(notifications.router)
api_router.include_router(audit.router)
