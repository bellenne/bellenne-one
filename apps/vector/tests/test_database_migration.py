import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.database import build_engine, init_database


def test_legacy_database_gains_account_owner_column_and_unique_index(settings):
    engine = build_engine(settings)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                name VARCHAR(160) NOT NULL,
                encrypted_token TEXT,
                token_hint VARCHAR(32),
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO accounts (id, name) VALUES (1, 'Legacy')"
        )

    init_database(engine)
    columns = {
        column["name"] for column in inspect(engine).get_columns("accounts")
    }
    assert "user_id" in columns

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, username, password_hash, created_at) "
            "VALUES (1, 'owner', 'hash', CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "UPDATE accounts SET user_id = 1 WHERE id = 1"
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO accounts (id, user_id, name) "
                "VALUES (2, 1, 'Duplicate owner')"
            )

    engine.dispose()
