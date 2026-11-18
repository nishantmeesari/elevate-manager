import logging
import os
from collections.abc import Generator

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


def _database_connect_kwargs(database_url: str) -> dict[str, str]:
    sslmode = os.environ.get("DATABASE_SSLMODE")
    if sslmode:
        return {"sslmode": sslmode}

    try:
        from urllib.parse import urlparse

        host = urlparse(database_url).hostname
    except (TypeError, ValueError):
        host = None
    if host and host not in {"localhost", "127.0.0.1", "::1"}:
        return {"sslmode": "require"}
    return {}


def _connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(
        database_url,
        row_factory=dict_row,
        **_database_connect_kwargs(database_url),
    )


def get_connection() -> Generator[psycopg.Connection, None, None]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="Database configuration is missing")

    try:
        with _connect(database_url) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            yield connection
    except psycopg.Error:
        logger.exception("Database operation failed")
        raise HTTPException(status_code=503, detail="Database unavailable") from None


def get_write_connection() -> Generator[psycopg.Connection, None, None]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="Database configuration is missing")

    try:
        with _connect(database_url) as connection:
            yield connection
    except psycopg.Error:
        logger.exception("Database operation failed")
        raise HTTPException(status_code=503, detail="Database unavailable") from None
