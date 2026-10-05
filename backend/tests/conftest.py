"""Shared fixtures. DB tests run against a separate `signalforge_test` database on the same server
(override with TEST_DATABASE_URL), migrated with Alembic, and are skipped if Postgres is down."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import BACKEND_DIR, get_settings
from signalforge.db.base import Base


def _test_url() -> str:
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url
    return (
        make_url(get_settings().database_url)
        .set(database="signalforge_test")
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = make_url(_test_url())
    admin = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 3},
    )
    try:
        with admin.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            )
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except OperationalError as exc:
        pytest.skip(f"Postgres unavailable: {exc.orig}")
    finally:
        admin.dispose()

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    rendered = url.render_as_string(hide_password=False)
    cfg.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
