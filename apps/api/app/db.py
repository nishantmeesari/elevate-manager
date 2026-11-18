import logging
import os
from collections.abc import Generator

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


def get_connection() -> Generator[psycopg.Connection, None, None]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=503, detail="Database configuration is missing")

    try:
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
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
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            yield connection
    except psycopg.Error:
        logger.exception("Database operation failed")
        raise HTTPException(status_code=503, detail="Database unavailable") from None
