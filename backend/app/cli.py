"""Command-line utilities: `python -m app.cli provision-tenant|seed|migrate-mgmt|...`.

Tenant databases are provisioned via `provision_tenant()` (create_all + stamp
"head" -- see its docstring for why that's used instead of replaying the
tenant migration chain against a brand-new database). For production, prefer
`migrate_tenant`/`migrate_all_tenants` for applying *new* migrations to
already-provisioned tenants.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import secrets
import sys
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (register all tables)
import app.models.mgmt  # noqa: F401  (register the tenants table)
from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.db import Base
from app.core.logging import configure_logging, get_logger
from app.core.mgmt_db import MgmtSyncSessionLocal
from app.core.security import hash_password
from app.models.catalog import DocumentType, TypeSchema
from app.models.constants import ROLE_ADMIN
from app.models.document import DocumentTypeDefaultPermission
from app.models.mgmt import PROVISIONING_READY
from app.models.mgmt import Tenant as MgmtTenant
from app.models.tenant import Group, User

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


def _tenant_url(tenant: "MgmtTenant") -> str:
    return (
        f"postgresql+psycopg2://{tenant.db_role}:{decrypt_secret(tenant.db_password_encrypted)}"
        f"@{tenant.db_host}:{tenant.db_port}/{tenant.db_name}"
    )


def migrate_tenant(slug: str) -> None:
    """Apply pending tenant-schema migrations to one already-provisioned
    tenant's database (new tenants get the current schema directly via
    provision_tenant's create_all+stamp; this is for rolling out a *new*
    migration to tenants provisioned before it existed)."""
    from alembic import command

    configure_logging(settings.log_level)
    mgmt_db = MgmtSyncSessionLocal()
    try:
        tenant = mgmt_db.query(MgmtTenant).filter(MgmtTenant.slug == slug).first()
        if tenant is None:
            print(f"no such tenant: {slug!r}")
            sys.exit(1)
        cfg = _alembic_config()
        cfg.set_main_option("sqlalchemy.url", _tenant_url(tenant))
        command.upgrade(cfg, "head")
        log.info("migrate_tenant_complete", slug=slug)
    finally:
        mgmt_db.close()


def migrate_all_tenants() -> None:
    """migrate_tenant, looped over every active tenant in the registry."""
    from alembic import command

    configure_logging(settings.log_level)
    mgmt_db = MgmtSyncSessionLocal()
    try:
        tenants = mgmt_db.query(MgmtTenant).filter(
            MgmtTenant.is_active.is_(True), MgmtTenant.provisioning_status == PROVISIONING_READY
        ).all()
        for tenant in tenants:
            cfg = _alembic_config()
            cfg.set_main_option("sqlalchemy.url", _tenant_url(tenant))
            command.upgrade(cfg, "head")
            log.info("migrate_tenant_complete", slug=tenant.slug)
        log.info("migrate_all_tenants_complete", count=len(tenants))
    finally:
        mgmt_db.close()


def _mgmt_alembic_config():
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(root, "alembic_mgmt.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "alembic_mgmt"))
    cfg.set_main_option("sqlalchemy.url", settings.mgmt_database_url_sync)
    return cfg


def migrate_mgmt() -> None:
    """Apply pending migrations to the management database (tenant registry)."""
    from alembic import command

    configure_logging(settings.log_level)
    command.upgrade(_mgmt_alembic_config(), "head")
    log.info("migrate_mgmt_complete")


def _admin_engine(database: str = "postgres"):
    """Platform-provisioning (admin) connection -- postgres_user/password
    repurposed as the credentials capable of CREATE ROLE/CREATE DATABASE,
    distinct in *role* from any tenant's own narrowly-scoped db_role even
    though in a single-server dev setup they share a server. AUTOCOMMIT since
    CREATE DATABASE/CREATE ROLE can't run inside a transaction."""
    admin_url = (
        f"postgresql+psycopg2://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{database}"
    )
    return create_engine(admin_url, isolation_level="AUTOCOMMIT")


def create_mgmt_db() -> None:
    """Create the management database itself if it doesn't exist yet.

    Needs its own top-level connection (to Postgres's `postgres` maintenance
    database) since the target database can't exist yet to connect to directly.
    """
    configure_logging(settings.log_level)
    engine = _admin_engine()
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": settings.mgmt_postgres_db},
        ).first()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{settings.mgmt_postgres_db}"'))
            log.info("mgmt_db_created", database=settings.mgmt_postgres_db)
        else:
            log.info("mgmt_db_already_exists", database=settings.mgmt_postgres_db)
        # No tenant role should be able to reach the management database at
        # all -- revoke Postgres's default PUBLIC connect grant (see the
        # matching revoke in provision_tenant for the same reasoning).
        conn.execute(text(f'REVOKE CONNECT ON DATABASE "{settings.mgmt_postgres_db}" FROM PUBLIC'))
    engine.dispose()


_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def provision_tenant(slug: str, name: str, seed: bool = False, id: uuid.UUID | None = None) -> None:
    """Create a brand-new tenant: a dedicated Postgres role + database (full
    tenant schema migrated in, owned by that role), a dedicated storage
    bucket/container, and its registry row in the management database.
    `seed=True` also creates a bootstrap admin/groups/document-types inside
    the new database (see `seed_tenant`). `id`, if given, becomes the
    tenant's id instead of a random uuid4 -- lets a legacy-data migration
    (see `migrate_legacy_tenant_data`) reuse the source tenant_id so copied
    rows need no remapping.

    Idempotent: safe to re-run after a partial failure (role/database left
    behind from a previous attempt are detected and reused, not recreated).
    Already-provisioned + already-seeded is a no-op; already-provisioned but
    not yet seeded (or re-run with seed=True) still runs seed_tenant, which is
    itself idempotent.
    """
    from alembic import command

    from app.storage import get_storage

    configure_logging(settings.log_level)
    if not _SLUG_RE.match(slug):
        print(f"slug must match {_SLUG_RE.pattern!r} (lowercase letters, digits, hyphens)")
        sys.exit(1)

    mgmt_db = MgmtSyncSessionLocal()
    try:
        tenant = mgmt_db.query(MgmtTenant).filter(MgmtTenant.slug == slug).first()
        if tenant is not None and tenant.provisioning_status == PROVISIONING_READY:
            log.info("tenant_already_provisioned", slug=slug)
            if seed:
                _seed_tenant_db(_tenant_url(tenant), tenant.id)
            return

        tenant_id = tenant.id if tenant is not None else (id or uuid.uuid4())

        safe = slug.replace("-", "_")
        db_name = f"aidocs_tenant_{safe}"
        db_role = f"tenant_{safe}"
        password = secrets.token_urlsafe(32)
        storage_container = f"aidocs-tenant-{slug}"

        # --- role + database (identifiers are f-string interpolated, safe
        # because _SLUG_RE restricts slug to [a-z0-9-] before any of this
        # runs; the password is a bound parameter, never embedded in SQL text) ---
        admin = _admin_engine()
        with admin.connect() as conn:
            role_exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": db_role}
            ).first()
            if not role_exists:
                conn.execute(text(f'CREATE ROLE "{db_role}" WITH LOGIN PASSWORD :pw'), {"pw": password})
                log.info("tenant_role_created", role=db_role)
            else:
                # Left over from a prior partial attempt -- reset the password
                # so the value we're about to encrypt and store is correct.
                conn.execute(text(f'ALTER ROLE "{db_role}" WITH PASSWORD :pw'), {"pw": password})

            db_exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
            ).first()
            if not db_exists:
                conn.execute(text(f'CREATE DATABASE "{db_name}" OWNER "{db_role}"'))
                log.info("tenant_database_created", database=db_name)
            # Postgres grants CONNECT on every new database to PUBLIC by
            # default -- every other tenant's role could otherwise open a
            # connection here (table-level ACLs would still block reading
            # data, but there's no reason to leave that surface open at all
            # given dedicated-role isolation was chosen specifically for
            # defense-in-depth). Only the owning role should be able to connect.
            conn.execute(text(f'REVOKE CONNECT ON DATABASE "{db_name}" FROM PUBLIC'))
        admin.dispose()

        # --- extension + schema privileges, on a connection to the new DB ---
        db_admin = _admin_engine(database=db_name)
        with db_admin.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text(f'GRANT ALL ON SCHEMA public TO "{db_role}"'))
        db_admin.dispose()

        # --- tenant-schema creation, as the tenant's own role so every table
        # it creates is owned by that role from the start. A brand-new DB is
        # built directly from the current model shape (create_all) rather
        # than by replaying 0001..0004: 0001 is a rename/backfill migration
        # written for an already-populated legacy database (it assumes
        # `documents` etc. already exist) and can't run against a genuinely
        # empty one. Stamping "head" afterward records it as fully migrated,
        # since create_all already produced the current shape. ---
        tenant_url = (
            f"postgresql+psycopg2://{db_role}:{password}"
            f"@{settings.postgres_host}:{settings.postgres_port}/{db_name}"
        )
        tenant_engine = create_engine(tenant_url, future=True)
        Base.metadata.create_all(tenant_engine)
        with tenant_engine.begin() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw ON embeddings "
                "USING hnsw (vector vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_chunks_fts ON chunks "
                "USING gin (to_tsvector('english', content))"
            ))
        tenant_engine.dispose()
        cfg = _alembic_config()
        cfg.set_main_option("sqlalchemy.url", tenant_url)
        command.stamp(cfg, "head")
        log.info("tenant_schema_created", database=db_name)

        # --- storage bucket/container ---
        stub = type("TenantStorageStub", (), {
            "storage_provider": settings.storage_provider, "storage_container": storage_container,
        })()
        get_storage(stub).ensure_container()
        log.info("tenant_storage_provisioned", container=storage_container)

        # --- register in the management database ---
        if tenant is None:
            tenant = MgmtTenant(id=tenant_id, name=name, slug=slug)
            mgmt_db.add(tenant)
        tenant.name = name
        tenant.is_active = True
        tenant.db_host = settings.postgres_host
        tenant.db_port = settings.postgres_port
        tenant.db_name = db_name
        tenant.db_role = db_role
        tenant.db_password_encrypted = encrypt_secret(password)
        tenant.db_sslmode = "prefer"
        tenant.storage_provider = settings.storage_provider
        tenant.storage_container = storage_container
        tenant.provisioning_status = PROVISIONING_READY
        tenant.provisioned_at = _dt.datetime.now(_dt.timezone.utc)
        mgmt_db.commit()
        log.info("tenant_provisioned", slug=slug, database=db_name, storage_container=storage_container)

        if seed:
            _seed_tenant_db(tenant_url, tenant_id)
    finally:
        mgmt_db.close()


def _slug(name: str) -> str:
    return "-".join(name.lower().split())


def seed_tenant(db, tenant_id: uuid.UUID) -> None:
    """Create a bootstrap admin/groups/document-types inside a tenant's own
    database. `db` is a Session already bound to that tenant's engine;
    `tenant_id` is that tenant's id from the management database (tenant_id
    columns here are plain UUIDs now, not FKs -- see models/tenant.py).
    Idempotent: get-or-create throughout, safe to call on an already-seeded
    or partially-seeded database.
    """
    admin = db.query(User).filter(User.tenant_id == tenant_id,
                                  User.email == settings.seed_admin_email.lower()).first()
    if admin is None:
        admin = User(tenant_id=tenant_id, email=settings.seed_admin_email.lower(), full_name="Administrator",
                     hashed_password=hash_password(settings.seed_admin_password), role=ROLE_ADMIN)
        db.add(admin)
        log.info("seeded_admin", email=admin.email)

    groups = {}
    for gname in ("Finance", "Legal"):
        g = db.query(Group).filter(Group.tenant_id == tenant_id, Group.name == gname).first()
        if g is None:
            g = Group(tenant_id=tenant_id, name=gname, description=f"{gname} team")
            db.add(g)
            db.flush()
        groups[gname] = g

    # Document types + their versioned field schemas
    seed_types = [("Contracts", CONTRACT_FIELDS, "Legal"), ("Invoices", INVOICE_FIELDS, "Finance")]
    for tname, fields, default_group in seed_types:
        dtype = db.query(DocumentType).filter(DocumentType.tenant_id == tenant_id,
                                               DocumentType.name == tname).first()
        if dtype is None:
            dtype = DocumentType(tenant_id=tenant_id, name=tname,
                                 description=f"{tname} documents", is_system=True)
            db.add(dtype)
            db.flush()
            schema = TypeSchema(tenant_id=tenant_id, document_type_id=dtype.id, version=1, fields=fields)
            db.add(schema)
            db.flush()
            dtype.active_schema_id = schema.id
            # Type-level default permission: the owning team gets read.
            db.add(DocumentTypeDefaultPermission(tenant_id=tenant_id, document_type_id=dtype.id,
                                                 group_id=groups[default_group].id, level="read"))
            log.info("seeded_document_type", document_type=tname)

    db.commit()
    log.info("seed_tenant_complete", login_email=settings.seed_admin_email)


def _seed_tenant_db(tenant_url: str, tenant_id: uuid.UUID) -> None:
    engine = create_engine(tenant_url, future=True)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    try:
        seed_tenant(db, tenant_id)
    finally:
        db.close()
        engine.dispose()


def seed() -> None:
    """Provision-if-missing, then seed, the one default dev tenant (matches
    this project's existing one-command local bootstrap UX)."""
    provision_tenant(_slug(settings.seed_tenant_name), settings.seed_tenant_name, seed=True)


# Tables with no tenant_id column of their own -- scoped transitively via a
# parent FK whose rows are already being migrated. (Confirmed exhaustive by
# inspecting Base.metadata: every other table carries tenant_id directly.)
_CHILD_TABLE_SCOPE = {
    "user_group": ("user_id", "users"),
    "document_type_links": ("document_id", "documents"),
    "messages": ("conversation_id", "conversations"),
    "pipeline_stages": ("run_id", "pipeline_runs"),
}


def migrate_legacy_tenant_data(dest_slug: str, dry_run: bool = True, legacy_bucket: str = "aidocs") -> None:
    """One-time migration of the original single-shared-database tenant's
    rows into its own dedicated database + storage container (provisioned
    separately via `provision-tenant`, ideally with `--id=<the legacy
    tenant_id>` so copied rows need no remapping).

    The source tenant is auto-detected as the sole distinct tenant_id in the
    legacy `users` table -- this only supports a legacy database that really
    is single-tenant; it errors out rather than guessing if that's not true.

    `--dry-run` (default): counts rows per table and reports, writes nothing.
    `--execute`: copies rows (PKs unchanged) and storage objects, then
    verifies row-count parity and a content-hash spot-check. The legacy
    database and bucket are never modified or deleted by this command --
    keep them as a rollback safety net for a soak period; teardown is a
    separate, later decision.
    """
    from sqlalchemy import insert
    from sqlalchemy import select as sa_select

    from app.storage import get_storage

    configure_logging(settings.log_level)

    mgmt_db = MgmtSyncSessionLocal()
    try:
        tenant = mgmt_db.query(MgmtTenant).filter(
            MgmtTenant.slug == dest_slug, MgmtTenant.provisioning_status == PROVISIONING_READY
        ).first()
        if tenant is None:
            print(f"tenant {dest_slug!r} is not provisioned (run provision-tenant first)")
            sys.exit(1)
        dest_url = _tenant_url(tenant)
        dest_storage_stub = type("DestStorageStub", (), {
            "storage_provider": tenant.storage_provider, "storage_container": tenant.storage_container,
        })()
    finally:
        mgmt_db.close()

    legacy_storage_stub = type("LegacyStorageStub", (), {
        "storage_provider": settings.storage_provider, "storage_container": legacy_bucket,
    })()

    legacy_engine = create_engine(settings.database_url_sync, future=True)
    dest_engine = create_engine(dest_url, future=True)

    with legacy_engine.connect() as legacy_conn:
        source_ids = [r[0] for r in legacy_conn.execute(text("SELECT DISTINCT tenant_id FROM users")).all()]
        if len(source_ids) != 1:
            print(f"expected exactly one tenant_id in the legacy users table, found {len(source_ids)}: "
                  f"{source_ids} -- this tool only supports a genuinely single-tenant legacy database")
            sys.exit(1)
        source_tenant_id = source_ids[0]
        log.info("legacy_source_tenant_resolved", tenant_id=str(source_tenant_id), dry_run=dry_run)

        report: dict[str, int] = {}
        copied_ids: dict[str, set] = {}          # table name -> copied PK values (single-column PK only)
        deferred_active_schema: dict = {}         # document_types.id -> its real active_schema_id
        document_version_rows: list[dict] = []    # for the storage copy pass afterward

        # document_types <-> type_schemas is a genuine circular FK
        # (active_schema_id points at a type_schemas row that itself points
        # back at the document_type via document_type_id). SQLAlchemy's
        # sorted_tables() correctly warns it can't order that pair -- but
        # empirically the disruption isn't limited to just those two tables:
        # type_schemas also FKs to users (created_by), and that edge came out
        # misordered too (type_schemas before users). Rather than trust
        # sorted_tables()'s order anywhere near the cyclic component, pin the
        # tables it touches to a manually-verified-safe order up front, then
        # fall back to sorted_tables()'s (otherwise correct) order for
        # everything else.
        pinned_order = ["users", "groups", "document_types", "type_schemas"]
        pinned_tables = [Base.metadata.tables[n] for n in pinned_order]
        remaining_tables = [t for t in Base.metadata.sorted_tables if t.name not in pinned_order]

        with dest_engine.connect() as dest_conn:
            for table in pinned_tables + remaining_tables:
                name = table.name
                cols = {c.name for c in table.columns}

                if "tenant_id" in cols:
                    stmt = sa_select(table).where(table.c.tenant_id == source_tenant_id)
                elif name in _CHILD_TABLE_SCOPE:
                    fk_col, parent = _CHILD_TABLE_SCOPE[name]
                    parent_ids = copied_ids.get(parent, set())
                    if not parent_ids:
                        report[name] = 0
                        continue
                    stmt = sa_select(table).where(table.c[fk_col].in_(parent_ids))
                else:
                    raise RuntimeError(
                        f"table {name!r} has no tenant_id and no entry in _CHILD_TABLE_SCOPE -- "
                        "a table was added since this migration was written; update the mapping")

                rows = [dict(r) for r in legacy_conn.execute(stmt).mappings().all()]
                report[name] = len(rows)
                if not rows:
                    continue

                if "id" in cols:
                    copied_ids[name] = {r["id"] for r in rows}
                if name == "document_versions":
                    document_version_rows.extend(rows)

                if dry_run:
                    continue

                # document_types <-> type_schemas is a genuine circular FK
                # (active_schema_id points at a type_schemas row that itself
                # points back at the document_type). Insert document_types
                # with active_schema_id nulled out first; type_schemas can
                # then insert cleanly; the real value gets patched in once
                # both tables exist below.
                if name == "document_types":
                    for r in rows:
                        if r.get("active_schema_id"):
                            deferred_active_schema[r["id"]] = r["active_schema_id"]
                            r["active_schema_id"] = None

                dest_conn.execute(insert(table), rows)

            if not dry_run and deferred_active_schema:
                dt_table = Base.metadata.tables["document_types"]
                for doc_type_id, schema_id in deferred_active_schema.items():
                    dest_conn.execute(
                        dt_table.update().where(dt_table.c.id == doc_type_id).values(active_schema_id=schema_id))

            if not dry_run:
                dest_conn.commit()

    legacy_engine.dispose()
    dest_engine.dispose()

    print(f"\n{'DRY RUN -- nothing written' if dry_run else 'EXECUTED'} (source tenant_id={source_tenant_id})")
    for name, count in report.items():
        if count:
            print(f"  {name:35s} {count}")
    total = sum(report.values())
    print(f"  {'TOTAL':35s} {total}")

    if dry_run:
        print("\nRe-run with dry_run=False (--execute) to actually copy this data and the "
              f"{len(document_version_rows)} associated storage object(s).")
        return

    # --- storage objects ---
    copied_objects = 0
    for dv in document_version_rows:
        data = get_storage(legacy_storage_stub).get_bytes(dv["storage_key"])
        get_storage(dest_storage_stub).put_object(dv["storage_key"], data, dv["mime_type"])
        copied_objects += 1
    log.info("legacy_storage_objects_copied", count=copied_objects)

    # --- verify: row-count parity + a content-hash spot-check ---
    mismatches = []
    with legacy_engine.connect() as legacy_conn, dest_engine.connect() as dest_conn:
        for name, expected in report.items():
            actual = dest_conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
            # Destination may have pre-existing rows from other sources (e.g.
            # a --seed run) in principle, so check >= rather than ==; for a
            # freshly-provisioned, unseeded tenant these will be exact.
            if actual < expected:
                mismatches.append((name, expected, actual))

    import hashlib

    hash_checked = hash_mismatches = 0
    for dv in document_version_rows[:5]:
        data = get_storage(dest_storage_stub).get_bytes(dv["storage_key"])
        actual_hash = hashlib.sha256(data).hexdigest()
        hash_checked += 1
        if actual_hash != dv["content_hash"]:
            hash_mismatches += 1
            log.error("legacy_migration_hash_mismatch", storage_key=dv["storage_key"])

    print(f"\nStorage objects copied: {copied_objects}")
    print(f"Row-count verification: {'OK' if not mismatches else f'MISMATCHES: {mismatches}'}")
    print(f"Content-hash spot-check: {hash_checked - hash_mismatches}/{hash_checked} matched")
    if mismatches or hash_mismatches:
        print("\n*** VERIFICATION FAILED -- investigate before trusting the migrated tenant. "
              "The legacy database/bucket are untouched, so nothing is lost. ***")
        sys.exit(1)
    print("\nMigration verified successfully.")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        seed()
    elif cmd == "migrate":
        migrate()
    elif cmd == "create-mgmt-db":
        create_mgmt_db()
    elif cmd == "migrate-mgmt":
        migrate_mgmt()
    elif cmd == "provision-tenant":
        if len(sys.argv) < 4:
            print("usage: python -m app.cli provision-tenant <slug> <name> [--seed] [--id=<uuid>]")
            sys.exit(1)
        extra = sys.argv[4:]
        id_arg = next((a.split("=", 1)[1] for a in extra if a.startswith("--id=")), None)
        provision_tenant(sys.argv[2], sys.argv[3], seed="--seed" in extra,
                         id=uuid.UUID(id_arg) if id_arg else None)
    elif cmd == "migrate-tenant":
        if len(sys.argv) < 3:
            print("usage: python -m app.cli migrate-tenant <slug>")
            sys.exit(1)
        migrate_tenant(sys.argv[2])
    elif cmd == "migrate-all-tenants":
        migrate_all_tenants()
    elif cmd == "migrate-legacy-tenant":
        if len(sys.argv) < 3:
            print("usage: python -m app.cli migrate-legacy-tenant <dest-slug> [--execute]")
            sys.exit(1)
        migrate_legacy_tenant_data(sys.argv[2], dry_run="--execute" not in sys.argv[3:])
    else:
        print("usage: python -m app.cli [seed|migrate|create-mgmt-db|migrate-mgmt|"
              "provision-tenant <slug> <name> [--seed]|migrate-tenant <slug>|migrate-all-tenants|"
              "migrate-legacy-tenant <dest-slug> [--execute]]")
        sys.exit(1)


if __name__ == "__main__":
    main()
