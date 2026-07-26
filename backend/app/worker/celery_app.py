"""Celery application.

Queues are split by resource profile so a 500-page OCR job can't starve
one-page invoice classification:
  * q_ocr   — slow, memory-heavy text extraction / understanding
  * q_llm   — rate-limit-bound model calls (classify/summarize/extract/embed)
  * q_light — chunking + bookkeeping

State lives in Postgres (`pipeline_run` / `pipeline_stage`), not the result
backend. `acks_late` + idempotent tasks give crash safety.
"""
from __future__ import annotations

from celery import Celery

from app.core.config import settings
from app.core.logging import configure_logging

configure_logging(settings.log_level)

celery_app = Celery(
    "aidocs",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=3600,
    broker_transport_options={"visibility_timeout": 3600},  # long OCR tasks
    task_default_queue="q_light",
    task_routes={
        "pipeline.text_extraction": {"queue": "q_ocr"},
        "pipeline.document_understanding": {"queue": "q_ocr"},
        "pipeline.classification": {"queue": "q_llm"},
        "pipeline.summarization": {"queue": "q_llm"},
        "pipeline.metadata_extraction": {"queue": "q_llm"},
        "pipeline.embedding": {"queue": "q_llm"},
        "pipeline.chunking": {"queue": "q_light"},
    },
)

# Ensure task modules are imported/registered.
celery_app.autodiscover_tasks(["app.worker"])
from app.worker import tasks  # noqa: E402,F401
