"""Administration: AI config, confidence thresholds, prompt versions."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_admin
from app.core.config import settings
from app.models.constants import AUDIT_CONFIG_CHANGE
from app.models.ops import AIConfig, PromptVersion
from app.models.tenant import User
from app.schemas import PromptVersionCreate, ThresholdUpdate
from app.services import audit

router = APIRouter(prefix="/admin", tags=["admin"])


async def _get_or_create_config(db, tenant_id) -> AIConfig:
    cfg = (await db.execute(select(AIConfig).where(AIConfig.tenant_id == tenant_id))).scalars().first()
    if cfg is None:
        cfg = AIConfig(tenant_id=tenant_id, provider_overrides={}, confidence_thresholds={},
                       processing_config={"chunk_tokens": settings.chunk_tokens,
                                          "chunk_overlap_tokens": settings.chunk_overlap_tokens,
                                          "max_upload_mb": settings.max_upload_mb})
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
    return cfg


@router.get("/config", summary="Get tenant AI configuration")
async def get_config(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    """Per-tenant overrides for the AI/embedding provider, confidence
    thresholds, and processing config, alongside the system-wide `defaults`
    they override. Auto-creates an empty config row on first call."""
    cfg = await _get_or_create_config(db, admin.tenant_id)
    return {
        "provider_overrides": cfg.provider_overrides,
        "confidence_thresholds": cfg.confidence_thresholds,
        "processing_config": cfg.processing_config,
        "defaults": {
            "ai_provider": settings.effective_ai_provider,
            "embedding_provider": settings.effective_embedding_provider,
            "classify_confidence_threshold": settings.classify_confidence_threshold,
            "extract_autoaccept_threshold": settings.extract_autoaccept_threshold,
            "ingestion_use_deepagents": settings.ingestion_use_deepagents,
        },
    }


@router.put("/config", summary="Update tenant AI configuration")
async def update_config(body: ThresholdUpdate, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    """Partial update -- any of `confidence_thresholds`, `provider_overrides`,
    `processing_config` left null (omitted) is left unchanged."""
    cfg = await _get_or_create_config(db, admin.tenant_id)
    if body.confidence_thresholds is not None:
        cfg.confidence_thresholds = body.confidence_thresholds
    if body.provider_overrides is not None:
        cfg.provider_overrides = body.provider_overrides
    if body.processing_config is not None:
        cfg.processing_config = body.processing_config
    await audit.log_event(db, tenant_id=admin.tenant_id, actor_id=admin.id, event_type=AUDIT_CONFIG_CHANGE,
                          object_type="ai_config", object_id=cfg.id,
                          detail={"keys": [k for k, v in body.model_dump().items() if v is not None]}, commit=False)
    await db.commit()
    return {"status": "updated"}


@router.get("/prompts", summary="List prompt versions")
async def list_prompts(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    """Both tenant-specific prompt overrides and the system-wide defaults
    (`tenant_id` null), newest version first per key."""
    rows = (await db.execute(
        select(PromptVersion).where((PromptVersion.tenant_id == admin.tenant_id) | (PromptVersion.tenant_id.is_(None)))
        .order_by(PromptVersion.key, PromptVersion.version.desc())
    )).scalars().all()
    return [{"id": str(p.id), "key": p.key, "version": p.version, "is_active": p.is_active,
             "template": p.template} for p in rows]


@router.post("/prompts", summary="Create a new prompt version")
async def create_prompt(body: PromptVersionCreate, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_tenant_db)):
    """Prompts are versioned and immutable, like document-type schemas --
    this always appends the next version number for the given `key` and
    marks it active, it never edits an existing version in place."""
    maxv = (await db.execute(
        select(func.coalesce(func.max(PromptVersion.version), 0)).where(
            PromptVersion.tenant_id == admin.tenant_id, PromptVersion.key == body.key)
    )).scalar() or 0
    p = PromptVersion(tenant_id=admin.tenant_id, key=body.key, version=maxv + 1, template=body.template, is_active=True)
    db.add(p)
    await audit.log_event(db, tenant_id=admin.tenant_id, actor_id=admin.id, event_type=AUDIT_CONFIG_CHANGE,
                          object_type="prompt", object_id=None, detail={"key": body.key, "version": maxv + 1}, commit=False)
    await db.commit()
    await db.refresh(p)
    return {"id": str(p.id), "key": p.key, "version": p.version}
