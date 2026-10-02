from pathlib import Path
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from .models import Base


def database(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {})
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
    return engine, sessionmaker(engine, expire_on_commit=False)


def migrate(engine):
    # Additive baseline migration, matching the existing modules' create_all convention.
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"))
        if not connection.scalar(text("SELECT version FROM schema_migrations WHERE version=1")):
            Base.metadata.create_all(connection)
            connection.execute(text("INSERT INTO schema_migrations(version) VALUES (1)"))
        if not connection.scalar(text("SELECT version FROM schema_migrations WHERE version=2")):
            additions = {
                "sync_policies": {
                    "price_source": "TEXT NOT NULL DEFAULT 'storefront'", "city": "TEXT NOT NULL DEFAULT 'Москва'",
                    "price_basis": "TEXT NOT NULL DEFAULT 'loyalty_price'", "context_version": "INTEGER NOT NULL DEFAULT 1",
                    "page_interval_seconds": "INTEGER NOT NULL DEFAULT 2",
                },
                "marketplace_products": {
                    "price_source": "TEXT NOT NULL DEFAULT 'seller_api'", "price_context_version": "INTEGER NOT NULL DEFAULT 0",
                    "collection_status": "TEXT NOT NULL DEFAULT 'not_collected'", "collection_error": "TEXT",
                },
                "price_snapshots": {
                    "source_kind": "TEXT NOT NULL DEFAULT 'seller_api'", "context_version": "INTEGER NOT NULL DEFAULT 0",
                    "context": "JSON NOT NULL DEFAULT '{}'", "page_url": "TEXT", "evidence_key": "TEXT",
                },
                "sync_jobs": {"target_product_id": "INTEGER REFERENCES marketplace_products(id)"},
            }
            for table, columns in additions.items():
                existing = {column['name'] for column in inspect(connection).get_columns(table)}
                for name, definition in columns.items():
                    if name not in existing:
                        connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
            Base.metadata.create_all(connection)
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_price_snapshots_source_kind ON price_snapshots(source_kind)"))
            # A new collection source needs an explicit new daily activation.
            # Historical Seller API snapshots remain intact and separately selectable.
            connection.execute(text("UPDATE sync_policies SET enabled=0"))
            connection.execute(text("INSERT INTO schema_migrations(version) VALUES (2)"))
        if not connection.scalar(text("SELECT version FROM schema_migrations WHERE version=3")):
            columns = {c['name'] for c in inspect(connection).get_columns("sync_policies")}
            if "browser_client_id" not in columns:
                connection.execute(text("ALTER TABLE sync_policies ADD COLUMN browser_client_id VARCHAR(36)"))
            Base.metadata.create_all(connection)
            connection.execute(text("INSERT INTO schema_migrations(version) VALUES (3)"))
