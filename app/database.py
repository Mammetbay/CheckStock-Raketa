"""Database connection and session management (Seam for PostgreSQL / Local test stand-in)."""

from __future__ import annotations

import os
from collections.abc import Generator
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


def resolve_database_url(raw_url: str | None = None) -> str:
    """Resolve and normalize the database connection URL.

    Uses PostgreSQL (`postgresql+psycopg://...`) when `DATABASE_URL` is configured
    (e.g. in Docker Compose or production), and supports local SQLite stand-in for
    zero-config local execution or isolated test runs.
    """
    url = (raw_url or os.getenv("DATABASE_URL") or "sqlite:///./checkstock.db").strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and "+psycopg" not in url and "+psycopg2" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


DATABASE_URL = resolve_database_url()


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy models."""


def build_engine(database_url: str = DATABASE_URL) -> Engine:
    """Create a SQLAlchemy Engine with appropriate dialect options."""
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    eng = create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        future=True,
    )
    if database_url.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        def _enable_sqlite_fks(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return eng


engine = build_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency providing a transactional database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
