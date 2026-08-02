"""Command-line utilities: `python -m app.cli init-db|seed`.

init-db is intentionally create_all + explicit index DDL for a fast MVP bootstrap.
For production, generate Alembic migrations (see alembic/ scaffold in README).
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import func, inspect, text

import app.models  # noqa: F401  (register all tables)
from app.core.config import settings
from app.core.db import Base, SyncSessionLocal, sync_engine
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.models.catalog import DocumentType, TypeSchema
from app.models.constants import ROLE_ADMIN
from app.models.document import DocumentTypeDefaultPermission
from app.models.tenant import Group, Tenant, User

log = get_logger("cli")


def _f(key, name, data_type, description, **extra):
    return {"key": key, "name": name, "data_type": data_type,
            "description": description, "required": False, **extra}


# Contracts show off a list-of-objects (a contract has many parties).
CONTRACT_FIELDS = [
    _f("effective_date", "Effective Date", "date", "Date the contract takes effect"),
    _f("expiration_date", "Expiration Date", "date", "Date the contract expires"),
    _f("parties", "Parties", "list", "Every legal entity party to the contract",
       item=_f("party", "Party", "object", "One party to the contract", fields=[
           _f("name", "Name", "string", "Legal name of the party"),
           _f("role", "Role", "string", "Their role, e.g. Licensor, Licensee, Buyer, Seller"),
           _f("address", "Address", "string", "Registered address, if stated"),
       ])),
    _f("contract_value", "Contract Value", "currency", "Total monetary value"),
    _f("governing_law", "Governing Law", "string", "Jurisdiction governing the contract"),
    _f("payment_terms", "Payment Terms", "string", "Payment schedule and terms"),
    _f("renewal_terms", "Renewal Terms", "string", "Auto-renewal / renewal conditions"),
    _f("termination_clause", "Termination Clause", "string", "Conditions for termination"),
]
# Invoices show off a list of line items.
INVOICE_FIELDS = [
    _f("invoice_number", "Invoice Number", "string", "Unique invoice identifier"),
    _f("vendor", "Vendor", "string", "Supplier / vendor name"),
    _f("invoice_date", "Invoice Date", "date", "Date the invoice was issued"),
    _f("due_date", "Due Date", "date", "Payment due date"),
    _f("currency", "Currency", "string", "Currency code, e.g. USD, INR"),
    _f("total_amount", "Total Amount", "currency", "Total amount due"),
    _f("tax_amount", "Tax Amount", "currency", "Tax portion of the total"),
    _f("line_items", "Line Items", "list", "Each billed line on the invoice",
       item=_f("line_item", "Line Item", "object", "One billed line", fields=[
           _f("description", "Description", "string", "What was billed"),
           _f("quantity", "Quantity", "number", "Units billed"),
           _f("unit_price", "Unit Price", "currency", "Price per unit"),
           _f("amount", "Amount", "currency", "Line total"),
       ])),
]


def _alembic_config():
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url_sync)
    return cfg


def migrate() -> None:
    """Apply pending migrations to an existing database."""
    from alembic import command

    configure_logging(settings.log_level)
    command.upgrade(_alembic_config(), "head")
    log.info("migrate_complete")


def init_db() -> None:
    configure_logging(settings.log_level)
    with sync_engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    # Existing database? Migrate it (renames preserve data). Fresh one? create_all
    # builds the current shape and we stamp it as already-migrated.
    from alembic import command

    inspector = inspect(sync_engine)
    existing = set(inspector.get_table_names())
    cfg = _alembic_config()
    if existing and "alembic_version" not in existing:
        if "categories" in existing or "extractions" in existing:
            log.info("migrating_legacy_schema")
            command.upgrade(cfg, "head")
        else:
            command.stamp(cfg, "head")
    Base.metadata.create_all(sync_engine)
    if "alembic_version" not in set(inspect(sync_engine).get_table_names()):
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")
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

        # Document types + their versioned field schemas
        seed_types = [("Contracts", CONTRACT_FIELDS, "Legal"), ("Invoices", INVOICE_FIELDS, "Finance")]
        for tname, fields, default_group in seed_types:
            dt = db.query(DocumentType).filter(DocumentType.tenant_id == tenant.id,
                                               DocumentType.name == tname).first()
            if dt is not None and dt.is_system:
                # A system type migrated from the pre-nesting schema still has a
                # flat field list. Mint the richer built-in version (new version,
                # so nothing already extracted is disturbed). Types a user has
                # customised already contain objects/lists and are left alone.
                active = db.get(TypeSchema, dt.active_schema_id) if dt.active_schema_id else None
                has_nested = any(f.get("data_type") in ("object", "list")
                                 for f in (active.fields if active else []))
                if active is not None and not has_nested:
                    maxv = db.query(func.max(TypeSchema.version)).filter(
                        TypeSchema.document_type_id == dt.id).scalar() or 0
                    upgraded = TypeSchema(tenant_id=tenant.id, document_type_id=dt.id,
                                          version=maxv + 1, fields=fields)
                    db.add(upgraded)
                    db.flush()
                    dt.active_schema_id = upgraded.id
                    log.info("upgraded_system_type_schema", document_type=tname, version=maxv + 1)
            if dt is None:
                dt = DocumentType(tenant_id=tenant.id, name=tname,
                                  description=f"{tname} documents", is_system=True)
                db.add(dt)
                db.flush()
                schema = TypeSchema(tenant_id=tenant.id, document_type_id=dt.id, version=1, fields=fields)
                db.add(schema)
                db.flush()
                dt.active_schema_id = schema.id
                # Type-level default permission: the owning team gets read.
                db.add(DocumentTypeDefaultPermission(tenant_id=tenant.id, document_type_id=dt.id,
                                                     group_id=groups[default_group].id, level="read"))
                log.info("seeded_document_type", document_type=tname)

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
    elif cmd == "migrate":
        migrate()
    else:
        print("usage: python -m app.cli [init-db|seed|migrate]")
        sys.exit(1)


if __name__ == "__main__":
    main()
