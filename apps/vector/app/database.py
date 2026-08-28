from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    pass


def build_engine(settings: Settings) -> Engine:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )

    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_database(engine: Engine) -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(engine)
    account_columns = {
        column["name"] for column in inspect(engine).get_columns("accounts")
    }
    user_columns = {
        column["name"] for column in inspect(engine).get_columns("users")
    }
    with engine.begin() as connection:
        if "user_id" not in account_columns:
            connection.exec_driver_sql(
                "ALTER TABLE accounts "
                "ADD COLUMN user_id INTEGER REFERENCES users(id)"
            )
        if "external_user_id" not in user_columns:
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN external_user_id INTEGER"
            )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_accounts_user_id_unique ON accounts(user_id) "
            "WHERE user_id IS NOT NULL"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_users_external_user_id ON users(external_user_id) "
            "WHERE external_user_id IS NOT NULL"
        )


def session_scope(
    session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
