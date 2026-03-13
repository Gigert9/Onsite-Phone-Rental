from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv_if_present() -> None:
    """Load environment variables from a local .env file if available.

    This keeps local development simple on Windows (no need to set $env:... each run).
    If python-dotenv isn't installed, this becomes a no-op.
    """

    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return

    root = Path(__file__).resolve().parents[1]
    env_file = root / ".env"
    if env_file.exists():
        load_dotenv(dotenv_path=env_file, override=False)


def env_optional(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None or value == "":
        return None
    return value


def env_int_optional(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


_load_dotenv_if_present()


def mssql_settings() -> tuple[str | None, int, str | None, str | None, str | None]:
    server = env_optional("MSSQL_SERVER")
    port = env_int_optional("MSSQL_PORT", 1433) or 1433
    database = env_optional("MSSQL_DATABASE")
    user = env_optional("MSSQL_USER")
    password = env_optional("MSSQL_PASSWORD")
    return server, port, database, user, password
