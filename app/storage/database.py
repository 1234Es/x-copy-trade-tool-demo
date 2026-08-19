"""Database engine setup. Swapping SQLite for PostgreSQL later is a
`DATABASE_URL` change -- nothing in `models.py` or `repository.py` assumes
SQLite specifically (Core, not SQLite-specific SQL).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from app.storage.models import metadata

PROJECT_ROOT = Path(__file__).parent.parent.parent


def create_db_engine(database_url: str) -> Engine:
    if ":memory:" in database_url:
        # A plain in-memory SQLite engine hands out a fresh empty database
        # to every new connection -- StaticPool keeps a single connection
        # alive so tests can write and read back within the same engine.
        engine = create_engine(
            database_url, future=True, poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        metadata.create_all(engine)
        return engine

    if database_url.startswith("sqlite:///"):
        relative_path = database_url.replace("sqlite:///", "", 1)
        db_path = PROJECT_ROOT / relative_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        database_url = f"sqlite:///{db_path}"
    engine = create_engine(database_url, future=True)
    metadata.create_all(engine)
    return engine
