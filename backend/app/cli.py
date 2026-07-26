"""Command-line utilities: `python -m app.cli init-db|seed`.

init-db is intentionally create_all + explicit index DDL for a fast MVP bootstrap.
For production, generate Alembic migrations (see alembic/ scaffold in README).
"""
from __future__ import annotations

import sys

from sqlalchemy import text

import app.models  # noqa: F401  (register all tables)
from app.core.config import settings
from app.core.db import Base, SyncSessionLocal, sync_engine
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.models.catalog import Category, ExtractionSchema
from app.models.constants import (
    ROLE_ADMIN,
    VT_CURRENCY,
    VT_DATE,
    VT_TEXT,
)
from app.models.document import CategoryDefaultPermission
from app.models.tenant import Group, Tenant, User

log = get_logger("cli")

CONTRACT_FIELDS = [
    {"name": "Effective Date", "type": VT_DATE, "description": "Date the contract takes effect"},
    {"name": "Expiration Date", "type": VT_DATE, "description": "Date the contract expires"},
    {"name": "Parties", "type": VT_TEXT, "description": "Legal entities party to the contract"},
    {"name": "Contract Value", "type": VT_CURRENCY, "description": "Total monetary value"},
    {"name": "Governing Law", "type": VT_TEXT, "description": "Jurisdiction governing the contract"},
    {"name": "Payment Terms", "type": VT_TEXT, "description": "Payment schedule and terms"},
    {"name": "Renewal Terms", "type": VT_TEXT, "description": "Auto-renewal / renewal conditions"},
    {"name": "Termination Clause", "type": VT_TEXT, "description": "Conditions for termination"},
]
INVOICE_FIELDS = [
    {"name": "Invoice Number", "type": VT_TEXT, "description": "Unique invoice identifier"},
    {"name": "Vendor", "type": VT_TEXT, "description": "Supplier / vendor name"},
    {"name": "Invoice Date", "type": VT_DATE, "description": "Date the invoice was issued"},
    {"name": "Due Date", "type": VT_DATE, "description": "Payment due date"},
    {"name": "Currency", "type": VT_TEXT, "description": "Currency code"},
    {"name": "Total Amount", "type": VT_CURRENCY, "description": "Total amount due"},
    {"name": "Tax Amount", "type": VT_CURRENCY, "description": "Tax portion of the total"},
]


def init_db() -> None:
    configure_logging(settings.log_level)
    with sync_engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as conn:
        # HNSW index (pgvector >= 0.8) for cosine similarity.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw ON embeddings "
            "USING hnsw (vector vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        ))
        # GIN index backing Postgres full-text keyword search over chunks.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_chunks_fts ON chunks "
            "USING gin (to_tsvector('english', content))"
        ))
    log.info("init_db_complete")


def _slug(name: str) -> str:
    return "-".join(name.lower().split())


def seed() -> None:
    configure_logging(settings.log_level)
    db = SyncSessionLocal()
    try:
        slug = _slug(settings.seed_tenant_name)
        tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
        if tenant is None:
            tenant = Tenant(name=settings.seed_tenant_name, slug=slug)
            db.add(tenant)
            db.flush()
            log.info("seeded_tenant", tenant=tenant.name)

        admin = db.query(User).filter(User.tenant_id == tenant.id,
                                      User.email == settings.seed_admin_email.lower()).first()
        if admin is None:
            admin = User(tenant_id=tenant.id, email=settings.seed_admin_email.lower(), full_name="Administrator",
                         hashed_password=hash_password(settings.seed_admin_password), role=ROLE_ADMIN)
            db.add(admin)
            log.info("seeded_admin", email=admin.email)

        # Groups
        groups = {}
        for gname in ("Finance", "Legal"):
            g = db.query(Group).filter(Group.tenant_id == tenant.id, Group.name == gname).first()
            if g is None:
                g = Group(tenant_id=tenant.id, name=gname, description=f"{gname} team")
                db.add(g)
                db.flush()
            groups[gname] = g

        # Categories + extraction schemas
        seed_cats = [("Contracts", CONTRACT_FIELDS, "Legal"), ("Invoices", INVOICE_FIELDS, "Finance")]
        for cname, fields, default_group in seed_cats:
            cat = db.query(Category).filter(Category.tenant_id == tenant.id, Category.name == cname).first()
            if cat is None:
                cat = Category(tenant_id=tenant.id, name=cname, description=f"{cname} documents")
                db.add(cat)
                db.flush()
                schema = ExtractionSchema(tenant_id=tenant.id, category_id=cat.id, version=1, fields=fields)
                db.add(schema)
                db.flush()
                cat.active_schema_id = schema.id
                # Category-level default permission: the owning team gets read.
                db.add(CategoryDefaultPermission(tenant_id=tenant.id, category_id=cat.id,
                                                 group_id=groups[default_group].id, level="read"))
                log.info("seeded_category", category=cname)

        db.commit()
        log.info("seed_complete", login_email=settings.seed_admin_email)
    finally:
        db.close()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init-db":
        init_db()
    elif cmd == "seed":
        seed()
    else:
        print("usage: python -m app.cli [init-db|seed]")
        sys.exit(1)


if __name__ == "__main__":
    main()
