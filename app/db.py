from __future__ import annotations

import contextlib
from typing import Any, Iterator

import pymssql

from . import config


def _connect() -> pymssql.Connection:
    server, port, database, user, password = config.mssql_settings()
    missing = [
        name
        for name, val in [
            ("MSSQL_SERVER", server),
            ("MSSQL_DATABASE", database),
            ("MSSQL_USER", user),
            ("MSSQL_PASSWORD", password),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            "Missing MSSQL configuration env vars: "
            + ", ".join(missing)
            + ". See .env.example."
        )

    return pymssql.connect(
        server=server,
        port=port,
        user=user,
        password=password,
        database=database,
        autocommit=False,
        charset="UTF-8",
    )


@contextlib.contextmanager
def get_conn() -> Iterator[pymssql.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def execute_insert_returning_id(sql: str, params: tuple[Any, ...] = ()) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql + "; SELECT CAST(SCOPE_IDENTITY() AS int) AS id;", params)
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Insert failed to return identity")
            return int(row[0])
