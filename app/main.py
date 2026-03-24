from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db
from . import config
from .excel_import import parse_totali_phone_rentals_xls


APP_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = APP_ROOT / "static"

app = FastAPI(title="Onsite Leads Phone Sign In/Out")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


PBKDF2_ITERATIONS = 210_000
PBKDF2_HASH_NAME = "sha256"

EVENT_TOKEN_HOURS = config.env_int_optional("EVENT_TOKEN_HOURS", 12) or 12
if EVENT_TOKEN_HOURS < 1:
    EVENT_TOKEN_HOURS = 12

# In-memory unlock tokens: { (event_id, token): expires_epoch }
_EVENT_TOKENS: dict[tuple[int, str], float] = {}

# Cache DB column support checks to avoid extra round-trips.
_COL_SUPPORT: dict[tuple[str, str], bool] = {}


def _db_has_column(table: str, column: str) -> bool:
    key = (table.lower(), column.lower())
    if key in _COL_SUPPORT:
        return _COL_SUPPORT[key]
    # COL_LENGTH returns NULL when column doesn't exist.
    row = db.fetch_one(
        "SELECT CASE WHEN COL_LENGTH(%s, %s) IS NULL THEN 0 ELSE 1 END AS has_col",
        (table, column),
    )
    ok = bool(row and int(row.get("has_col") or 0) == 1)
    _COL_SUPPORT[key] = ok
    return ok


def _clean_expired_tokens(now: float | None = None) -> None:
    ts = now if now is not None else time.time()
    expired = [k for k, exp in _EVENT_TOKENS.items() if exp <= ts]
    for k in expired:
        _EVENT_TOKENS.pop(k, None)


def _issue_event_token(event_id: int, hours: int = EVENT_TOKEN_HOURS) -> str:
    _clean_expired_tokens()
    token = secrets.token_urlsafe(24)
    _EVENT_TOKENS[(event_id, token)] = time.time() + (hours * 3600)
    return token


def _require_event_token(event_id: int, token: str | None) -> None:
    _clean_expired_tokens()
    if not token:
        raise HTTPException(status_code=401, detail="Event password required")
    exp = _EVENT_TOKENS.get((event_id, token))
    if not exp or exp <= time.time():
        raise HTTPException(status_code=401, detail="Event password required")


def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(PBKDF2_HASH_NAME, password.encode("utf-8"), salt, iterations)


def _has_password(event_id: int) -> bool:
    row = db.fetch_one(
        "SELECT password_hash FROM dbo.events WHERE event_id=%s",
        (event_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return row["password_hash"] is not None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _decode_data_url_png(data_url: str) -> bytes:
    if not data_url or not data_url.startswith("data:"):
        raise ValueError("Invalid signature data")
    try:
        header, b64 = data_url.split(",", 1)
    except ValueError as e:
        raise ValueError("Invalid signature data") from e
    if "image/png" not in header:
        raise ValueError("Signature must be PNG")
    return base64.b64decode(b64)


def _make_display_name(name: str, booth: str | None) -> str:
    booth_clean = (booth or "").strip() or None
    if booth_clean:
        return f"{name} / {booth_clean}"
    return name


@app.get("/api/events")
def list_events() -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT
            event_id,
            name,
            created_at,
            CASE WHEN password_hash IS NULL THEN CAST(0 AS bit) ELSE CAST(1 AS bit) END AS has_password
        FROM dbo.events
        ORDER BY created_at DESC
        """
    )


@app.post("/api/events")
def create_event(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Event name is required")

    event_id = db.execute_insert_returning_id(
        "INSERT INTO dbo.events (name) VALUES (%s)",
        (name,),
    )
    return {"event_id": event_id, "name": name}


@app.delete("/api/events/{event_id}")
def delete_event(event_id: int) -> dict[str, Any]:
    # Destructive operation: permanently removes the event and all related rows.
    with db.get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            cur.execute("SELECT event_id FROM dbo.events WHERE event_id=%s", (event_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Event not found")

            cur.execute("DELETE FROM dbo.event_exhibitors WHERE event_id=%s", (event_id,))
            cur.execute("DELETE FROM dbo.events WHERE event_id=%s", (event_id,))
    return {"ok": True}


@app.post("/api/events/{event_id}/set-password")
def set_event_password(event_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    password = str(payload.get("password") or "")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    # Ensure event exists and isn't already protected
    row = db.fetch_one(
        "SELECT password_hash FROM dbo.events WHERE event_id=%s",
        (event_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    if row["password_hash"] is not None:
        raise HTTPException(status_code=409, detail="Event password already set")

    salt = secrets.token_bytes(16)
    pwd_hash = _hash_password(password, salt, PBKDF2_ITERATIONS)
    db.execute(
        """
        UPDATE dbo.events
        SET password_salt=%s, password_hash=%s, password_iterations=%s
        WHERE event_id=%s
        """,
        (salt, pwd_hash, PBKDF2_ITERATIONS, event_id),
    )
    return {"ok": True}


@app.post("/api/events/{event_id}/unlock")
def unlock_event(event_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    password = str(payload.get("password") or "")
    row = db.fetch_one(
        """
        SELECT password_salt, password_hash, password_iterations
        FROM dbo.events
        WHERE event_id=%s
        """,
        (event_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")

    if row["password_hash"] is None:
        raise HTTPException(status_code=409, detail="Event password not set")

    salt = bytes(row["password_salt"])
    expected_hash = bytes(row["password_hash"])
    iterations = int(row["password_iterations"] or PBKDF2_ITERATIONS)
    got = _hash_password(password, salt, iterations)
    if not secrets.compare_digest(got, expected_hash):
        raise HTTPException(status_code=401, detail="Incorrect password")

    return {"token": _issue_event_token(event_id)}


@app.post("/api/events/{event_id}/import-excel")
def import_excel(
    event_id: int,
    file: UploadFile = File(...),
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
) -> dict[str, Any]:
    _require_event_token(event_id, x_event_token)
    if not file.filename:
        raise HTTPException(status_code=400, detail="File is required")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".xls", ".xlsx"}:
        raise HTTPException(status_code=400, detail="Please upload an Excel file")

    tmp_dir = APP_ROOT / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"import_{event_id}_{int(_utc_now().timestamp())}{suffix}"
    with tmp_path.open("wb") as f:
        f.write(file.file.read())

    try:
        imported = parse_totali_phone_rentals_xls(str(tmp_path))
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    # Ensure event exists
    ev = db.fetch_one("SELECT event_id FROM dbo.events WHERE event_id=%s", (event_id,))
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")

    created = 0
    updated = 0

    for item in imported:
        # Upsert exhibitor
        existing_exh = db.fetch_one(
            "SELECT exhibitor_id FROM dbo.exhibitors WHERE name=%s AND ISNULL(booth,'')=ISNULL(%s,'')",
            (item.name, item.booth),
        )
        if existing_exh:
            exhibitor_id = int(existing_exh["exhibitor_id"])
            # keep display_name fresh
            db.execute(
                "UPDATE dbo.exhibitors SET display_name=%s WHERE exhibitor_id=%s",
                (item.display_name, exhibitor_id),
            )
        else:
            exhibitor_id = db.execute_insert_returning_id(
                "INSERT INTO dbo.exhibitors (display_name, name, booth) VALUES (%s,%s,%s)",
                (item.display_name, item.name, item.booth),
            )

        existing_link = db.fetch_one(
            "SELECT event_exhibitor_id FROM dbo.event_exhibitors WHERE event_id=%s AND exhibitor_id=%s",
            (event_id, exhibitor_id),
        )
        if existing_link:
            db.execute(
                "UPDATE dbo.event_exhibitors SET reserved_phones=%s, reserved_licenses=%s WHERE event_id=%s AND exhibitor_id=%s",
                (item.reserved_phones, item.reserved_licenses, event_id, exhibitor_id),
            )
            updated += 1
        else:
            db.execute(
                "INSERT INTO dbo.event_exhibitors (event_id, exhibitor_id, reserved_phones, reserved_licenses) VALUES (%s,%s,%s,%s)",
                (event_id, exhibitor_id, item.reserved_phones, item.reserved_licenses),
            )
            created += 1

    return {"imported_rows": len(imported), "created": created, "updated": updated}


@app.get("/api/events/{event_id}/exhibitors")
def list_event_exhibitors(
    event_id: int,
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
) -> list[dict[str, Any]]:
    _require_event_token(event_id, x_event_token)

    has_sig_snapshot = _db_has_column("dbo.event_exhibitors", "dropoff_signature") and _db_has_column(
        "dbo.event_exhibitors", "pickup_signature"
    )
    has_phone_ids = _db_has_column("dbo.event_exhibitors", "dropoff_phone_ids")
    has_chargers = _db_has_column("dbo.event_exhibitors", "dropoff_confirmed_chargers") and _db_has_column(
        "dbo.event_exhibitors", "pickup_confirmed_chargers"
    )

    extra_cols: list[str] = []
    if has_phone_ids:
        extra_cols.append("ee.dropoff_phone_ids")
    if has_chargers:
        extra_cols.append("ee.dropoff_confirmed_chargers")
        extra_cols.append("ee.pickup_confirmed_chargers")
    if has_sig_snapshot:
        extra_cols.append(
            "CASE WHEN ee.dropoff_signature IS NULL AND ee.pickup_signature IS NULL THEN CAST(0 AS bit) ELSE CAST(1 AS bit) END AS has_signature"
        )
    select_extra = (",\n            " + ",\n            ".join(extra_cols)) if extra_cols else ""

    rows = db.fetch_all(
        f"""
        SELECT
            ee.event_exhibitor_id,
            e.exhibitor_id,
            e.display_name,
            e.name,
            e.booth,
            ee.reserved_phones,
            ee.dropoff_confirmed_phones,
            ee.dropoff_at,
            ee.dropoff_note,
            ee.pickup_confirmed_phones,
            ee.pickup_at,
            ee.pickup_note
            {select_extra}
        FROM dbo.event_exhibitors ee
        JOIN dbo.exhibitors e ON e.exhibitor_id = ee.exhibitor_id
        WHERE ee.event_id = %s
        ORDER BY e.display_name ASC
        """,
        (event_id,),
    )
    return rows


@app.post("/api/events/{event_id}/exhibitors")
def add_event_exhibitor(
    event_id: int,
    payload: dict[str, Any],
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
) -> dict[str, Any]:
    _require_event_token(event_id, x_event_token)

    name = str(payload.get("name") or "").strip()
    booth = str(payload.get("booth") or "").strip() or None
    reserved_phones = payload.get("reserved_phones")

    if not name:
        raise HTTPException(status_code=400, detail="Exhibitor name is required")

    try:
        reserved_phones_int = int(reserved_phones)
    except Exception:
        raise HTTPException(status_code=400, detail="reserved_phones must be an integer")
    if reserved_phones_int < 0:
        raise HTTPException(status_code=400, detail="reserved_phones must be >= 0")

    display_name = _make_display_name(name, booth)

    with db.get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            cur.execute("SELECT event_id FROM dbo.events WHERE event_id=%s", (event_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Event not found")

            cur.execute(
                "SELECT exhibitor_id FROM dbo.exhibitors WHERE name=%s AND ISNULL(booth,'')=ISNULL(%s,'')",
                (name, booth),
            )
            row = cur.fetchone()
            if row:
                exhibitor_id = int(row["exhibitor_id"])
                cur.execute(
                    "UPDATE dbo.exhibitors SET display_name=%s WHERE exhibitor_id=%s",
                    (display_name, exhibitor_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO dbo.exhibitors (display_name, name, booth)
                    VALUES (%s,%s,%s);
                    SELECT CAST(SCOPE_IDENTITY() AS int) AS id;
                    """,
                    (display_name, name, booth),
                )
                exhibitor_id = int(cur.fetchone()["id"])

            cur.execute(
                "SELECT event_exhibitor_id FROM dbo.event_exhibitors WHERE event_id=%s AND exhibitor_id=%s",
                (event_id, exhibitor_id),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Exhibitor already exists for this event")

            cur.execute(
                """
                INSERT INTO dbo.event_exhibitors (event_id, exhibitor_id, reserved_phones, reserved_licenses)
                VALUES (%s,%s,%s,NULL);
                SELECT CAST(SCOPE_IDENTITY() AS int) AS id;
                """,
                (event_id, exhibitor_id, reserved_phones_int),
            )
            event_exhibitor_id = int(cur.fetchone()["id"])

            cur.execute(
                """
                SELECT
                    ee.event_exhibitor_id,
                    e.exhibitor_id,
                    e.display_name,
                    e.name,
                    e.booth,
                    ee.reserved_phones,
                    ee.dropoff_confirmed_phones,
                    ee.dropoff_at,
                    ee.dropoff_note,
                    ee.pickup_confirmed_phones,
                    ee.pickup_at,
                    ee.pickup_note
                FROM dbo.event_exhibitors ee
                JOIN dbo.exhibitors e ON e.exhibitor_id = ee.exhibitor_id
                WHERE ee.event_exhibitor_id=%s
                """,
                (event_exhibitor_id,),
            )
            created = cur.fetchone()

    if not created:
        raise HTTPException(status_code=500, detail="Failed to create exhibitor")
    return created


@app.patch("/api/event-exhibitors/{event_exhibitor_id}")
def update_event_exhibitor(
    event_exhibitor_id: int,
    payload: dict[str, Any],
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    booth = str(payload.get("booth") or "").strip() or None
    if not name:
        raise HTTPException(status_code=400, detail="Exhibitor name is required")
    display_name = _make_display_name(name, booth)

    with db.get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(
                "SELECT event_id, exhibitor_id FROM dbo.event_exhibitors WHERE event_exhibitor_id=%s",
                (event_exhibitor_id,),
            )
            ee = cur.fetchone()
            if not ee:
                raise HTTPException(status_code=404, detail="Record not found")
            event_id = int(ee["event_id"])
            _require_event_token(event_id, x_event_token)

            # Find-or-create the target exhibitor row (avoid impacting other events by editing shared rows).
            cur.execute(
                "SELECT exhibitor_id FROM dbo.exhibitors WHERE name=%s AND ISNULL(booth,'')=ISNULL(%s,'')",
                (name, booth),
            )
            row = cur.fetchone()
            if row:
                new_exhibitor_id = int(row["exhibitor_id"])
                cur.execute(
                    "UPDATE dbo.exhibitors SET display_name=%s WHERE exhibitor_id=%s",
                    (display_name, new_exhibitor_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO dbo.exhibitors (display_name, name, booth)
                    VALUES (%s,%s,%s);
                    SELECT CAST(SCOPE_IDENTITY() AS int) AS id;
                    """,
                    (display_name, name, booth),
                )
                new_exhibitor_id = int(cur.fetchone()["id"])

            cur.execute(
                """
                SELECT event_exhibitor_id
                FROM dbo.event_exhibitors
                WHERE event_id=%s AND exhibitor_id=%s AND event_exhibitor_id<>%s
                """,
                (event_id, new_exhibitor_id, event_exhibitor_id),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Another exhibitor row already uses that name/booth")

            cur.execute(
                "UPDATE dbo.event_exhibitors SET exhibitor_id=%s WHERE event_exhibitor_id=%s",
                (new_exhibitor_id, event_exhibitor_id),
            )
            if cur.rowcount <= 0:
                raise HTTPException(status_code=404, detail="Record not found")

            cur.execute(
                """
                SELECT
                    ee.event_exhibitor_id,
                    e.exhibitor_id,
                    e.display_name,
                    e.name,
                    e.booth,
                    ee.reserved_phones,
                    ee.dropoff_confirmed_phones,
                    ee.dropoff_at,
                    ee.dropoff_note,
                    ee.pickup_confirmed_phones,
                    ee.pickup_at,
                    ee.pickup_note
                FROM dbo.event_exhibitors ee
                JOIN dbo.exhibitors e ON e.exhibitor_id = ee.exhibitor_id
                WHERE ee.event_exhibitor_id=%s
                """,
                (event_exhibitor_id,),
            )
            updated = cur.fetchone()

    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update exhibitor")
    return updated


@app.delete("/api/event-exhibitors/{event_exhibitor_id}")
def delete_event_exhibitor(
    event_exhibitor_id: int,
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
) -> dict[str, Any]:
    with db.get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            cur.execute(
                """
                SELECT event_id, exhibitor_id, dropoff_confirmed_phones
                FROM dbo.event_exhibitors
                WHERE event_exhibitor_id=%s
                """,
                (event_exhibitor_id,),
            )
            ee = cur.fetchone()
            if not ee:
                raise HTTPException(status_code=404, detail="Record not found")
            event_id = int(ee["event_id"])
            exhibitor_id = int(ee["exhibitor_id"])
            dropped = int(ee["dropoff_confirmed_phones"] or 0)
            _require_event_token(event_id, x_event_token)

            cur.execute(
                "SELECT COUNT(1) AS cnt FROM dbo.event_exhibitor_actions WHERE event_exhibitor_id=%s",
                (event_exhibitor_id,),
            )
            actions_cnt = int(cur.fetchone()["cnt"])
            if dropped > 0 or actions_cnt > 0:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot delete exhibitor once any sign out/sign in has been recorded",
                )

            # FK requires actions deleted first (should be none, but keep safe).
            cur.execute("DELETE FROM dbo.event_exhibitor_actions WHERE event_exhibitor_id=%s", (event_exhibitor_id,))
            cur.execute("DELETE FROM dbo.event_exhibitors WHERE event_exhibitor_id=%s", (event_exhibitor_id,))
            if cur.rowcount <= 0:
                raise HTTPException(status_code=404, detail="Record not found")

            # Optional cleanup: remove orphan exhibitor rows.
            cur.execute(
                "SELECT TOP 1 event_exhibitor_id FROM dbo.event_exhibitors WHERE exhibitor_id=%s",
                (exhibitor_id,),
            )
            if not cur.fetchone():
                cur.execute("DELETE FROM dbo.exhibitors WHERE exhibitor_id=%s", (exhibitor_id,))

    return {"ok": True}


@app.get("/api/event-exhibitors/{event_exhibitor_id}/signature/{sig_type}")
def get_event_exhibitor_signature(
    event_exhibitor_id: int,
    sig_type: str,
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
):
    sig_type_clean = (sig_type or "").strip().lower()
    if sig_type_clean not in {"dropoff", "pickup"}:
        raise HTTPException(status_code=400, detail="sig_type must be dropoff or pickup")

    col = "dropoff_signature" if sig_type_clean == "dropoff" else "pickup_signature"
    row = db.fetch_one(
        f"SELECT event_id, {col} AS sig FROM dbo.event_exhibitors WHERE event_exhibitor_id=%s",
        (event_exhibitor_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Record not found")
    _require_event_token(int(row["event_id"]), x_event_token)

    sig = row.get("sig")
    if sig is None:
        raise HTTPException(status_code=404, detail="Signature not found")

    return Response(content=bytes(sig), media_type="image/png")


@app.post("/api/event-exhibitors/{event_exhibitor_id}/dropoff")
def dropoff(
    event_exhibitor_id: int,
    payload: dict[str, Any],
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
) -> dict[str, Any]:
    confirmed = payload.get("confirmed_phones")
    printed_name = str(payload.get("printed_name") or "").strip()
    signature = str(payload.get("signature") or "").strip()
    note = str(payload.get("note") or "").strip()
    phone_ids = str(payload.get("phone_ids") or "").strip()

    charger_included = bool(payload.get("charger_included") or False)
    charger_qty_raw = payload.get("charger_qty")

    try:
        confirmed_int = int(confirmed)
    except Exception:
        raise HTTPException(status_code=400, detail="confirmed_phones must be an integer")

    if confirmed_int < 0:
        raise HTTPException(status_code=400, detail="confirmed_phones must be >= 0")
    if not printed_name:
        raise HTTPException(status_code=400, detail="Printed name is required")

    has_parent_phone_ids = _db_has_column("dbo.event_exhibitors", "dropoff_phone_ids")
    if confirmed_int > 0:
        if not phone_ids:
            raise HTTPException(status_code=400, detail="Phone ID numbers are required when dropping off phones")
        # Accept one-per-line, but also tolerate comma/semicolon separation.
        import re

        parsed_ids = [t.strip() for t in re.split(r"[\r\n,;]+", phone_ids) if t and t.strip()]
        if len(parsed_ids) != confirmed_int:
            raise HTTPException(
                status_code=400,
                detail=f"Please provide exactly {confirmed_int} phone ID number(s). Got {len(parsed_ids)}.",
            )
        if not has_parent_phone_ids:
            raise HTTPException(
                status_code=409,
                detail="Database is missing dropoff_phone_ids column. Please run database/setup.sql schema upgrades.",
            )

    charger_qty: int = 0
    if charger_included:
        try:
            charger_qty = int(charger_qty_raw)
        except Exception:
            raise HTTPException(status_code=400, detail="charger_qty must be an integer")
        if charger_qty <= 0:
            raise HTTPException(status_code=400, detail="charger_qty must be >= 1 when charger_included is true")

    row = db.fetch_one(
        """
        SELECT event_id, reserved_phones, dropoff_confirmed_phones
        FROM dbo.event_exhibitors
        WHERE event_exhibitor_id=%s
        """,
        (event_exhibitor_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Record not found")
    _require_event_token(int(row["event_id"]), x_event_token)
    expected = int(row["reserved_phones"])
    prev_total = int(row["dropoff_confirmed_phones"] or 0)
    new_total = prev_total + confirmed_int
    if new_total > expected and not note:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Discrepancy: reserved {expected}, already signed out {prev_total}, "
                f"signing out {confirmed_int} (new total {new_total}). Note is required to continue."
            ),
        )

    try:
        sig_bytes = _decode_data_url_png(signature)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    now = _utc_now()

    has_action_phone_ids = _db_has_column("dbo.event_exhibitor_actions", "phone_ids")
    has_action_charger_qty = _db_has_column("dbo.event_exhibitor_actions", "charger_qty")
    has_parent_chargers = _db_has_column("dbo.event_exhibitors", "dropoff_confirmed_chargers")

    with db.get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            # Insert signed action record
            cols = ["event_exhibitor_id", "action_type", "quantity", "action_at", "printed_name", "signature", "note"]
            vals: list[Any] = [event_exhibitor_id, "dropoff", confirmed_int, now, printed_name, sig_bytes, note or None]
            if has_action_phone_ids:
                cols.insert(6, "phone_ids")
                vals.insert(6, phone_ids or None)
            if has_action_charger_qty:
                cols.insert(6, "charger_qty")
                vals.insert(6, charger_qty if charger_included else None)

            placeholders = ", ".join(["%s"] * len(cols))
            col_sql = ", ".join(cols)
            cur.execute(
                f"INSERT INTO dbo.event_exhibitor_actions ({col_sql}) VALUES ({placeholders})",
                tuple(vals),
            )

            # Update fast-path totals + keep last-action snapshot on the parent row.
            update_sets = [
                "dropoff_confirmed_phones=%s",
                "dropoff_at=%s",
                "dropoff_printed_name=%s",
                "dropoff_signature=%s",
                "dropoff_note=%s",
            ]
            update_vals: list[Any] = [new_total, now, printed_name, sig_bytes, note or None]

            if has_parent_phone_ids and phone_ids:
                prev = db.fetch_one(
                    "SELECT dropoff_phone_ids FROM dbo.event_exhibitors WHERE event_exhibitor_id=%s",
                    (event_exhibitor_id,),
                )
                prev_txt = str((prev or {}).get("dropoff_phone_ids") or "").strip()
                combined = phone_ids if not prev_txt else (prev_txt if phone_ids in prev_txt else (prev_txt + "\n" + phone_ids))
                update_sets.append("dropoff_phone_ids=%s")
                update_vals.append(combined)

            if has_parent_chargers:
                prev_c = db.fetch_one(
                    "SELECT dropoff_confirmed_chargers FROM dbo.event_exhibitors WHERE event_exhibitor_id=%s",
                    (event_exhibitor_id,),
                )
                prev_ch = int((prev_c or {}).get("dropoff_confirmed_chargers") or 0)
                new_ch = prev_ch + (charger_qty if charger_included else 0)
                update_sets.append("dropoff_confirmed_chargers=%s")
                update_vals.append(new_ch)

            update_vals.append(event_exhibitor_id)
            cur.execute(
                f"UPDATE dbo.event_exhibitors SET {', '.join(update_sets)} WHERE event_exhibitor_id=%s",
                tuple(update_vals),
            )
            if cur.rowcount <= 0:
                raise HTTPException(status_code=404, detail="Record not found")

    return {"ok": True, "dropoff_at": now.isoformat()}


@app.post("/api/event-exhibitors/{event_exhibitor_id}/pickup")
def pickup(
    event_exhibitor_id: int,
    payload: dict[str, Any],
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
) -> dict[str, Any]:
    confirmed = payload.get("confirmed_phones")
    printed_name = str(payload.get("printed_name") or "").strip()
    signature = str(payload.get("signature") or "").strip()
    note = str(payload.get("note") or "").strip()
    confirmed_chargers_raw = payload.get("confirmed_chargers")

    try:
        confirmed_int = int(confirmed)
    except Exception:
        raise HTTPException(status_code=400, detail="confirmed_phones must be an integer")

    if confirmed_int < 0:
        raise HTTPException(status_code=400, detail="confirmed_phones must be >= 0")
    if not printed_name:
        raise HTTPException(status_code=400, detail="Printed name is required")

    confirmed_chargers = 0
    if confirmed_chargers_raw is not None:
        try:
            confirmed_chargers = int(confirmed_chargers_raw)
        except Exception:
            raise HTTPException(status_code=400, detail="confirmed_chargers must be an integer")
        if confirmed_chargers < 0:
            raise HTTPException(status_code=400, detail="confirmed_chargers must be >= 0")

    row = db.fetch_one(
        """
        SELECT event_id, reserved_phones, dropoff_confirmed_phones, pickup_confirmed_phones
        FROM dbo.event_exhibitors
        WHERE event_exhibitor_id=%s
        """,
        (event_exhibitor_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Record not found")
    _require_event_token(int(row["event_id"]), x_event_token)
    reserved = int(row["reserved_phones"])
    expected = int(row["dropoff_confirmed_phones"]) if row["dropoff_confirmed_phones"] is not None else reserved
    prev_total = int(row["pickup_confirmed_phones"] or 0)
    new_total = prev_total + confirmed_int
    if new_total > expected and not note:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Discrepancy: expected sign in {expected}, already signed in {prev_total}, "
                f"signing in {confirmed_int} (new total {new_total}). Note is required to continue."
            ),
        )

    try:
        sig_bytes = _decode_data_url_png(signature)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    now = _utc_now()

    has_action_charger_qty = _db_has_column("dbo.event_exhibitor_actions", "charger_qty")
    has_parent_chargers = _db_has_column("dbo.event_exhibitors", "pickup_confirmed_chargers")
    expected_chargers = 0
    if has_parent_chargers:
        exp_row = db.fetch_one(
            "SELECT dropoff_confirmed_chargers, pickup_confirmed_chargers FROM dbo.event_exhibitors WHERE event_exhibitor_id=%s",
            (event_exhibitor_id,),
        )
        expected_chargers = int((exp_row or {}).get("dropoff_confirmed_chargers") or 0)
        prev_pick_ch = int((exp_row or {}).get("pickup_confirmed_chargers") or 0)
        if prev_pick_ch + confirmed_chargers > expected_chargers and not note:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Discrepancy: expected charger sign in {expected_chargers}, already signed in {prev_pick_ch}, "
                    f"signing in {confirmed_chargers} (new total {prev_pick_ch + confirmed_chargers}). Note is required to continue."
                ),
            )

    with db.get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            cols = ["event_exhibitor_id", "action_type", "quantity", "action_at", "printed_name", "signature", "note"]
            vals: list[Any] = [event_exhibitor_id, "pickup", confirmed_int, now, printed_name, sig_bytes, note or None]
            if has_action_charger_qty:
                cols.insert(6, "charger_qty")
                vals.insert(6, confirmed_chargers if confirmed_chargers > 0 else None)

            placeholders = ", ".join(["%s"] * len(cols))
            col_sql = ", ".join(cols)
            cur.execute(
                f"INSERT INTO dbo.event_exhibitor_actions ({col_sql}) VALUES ({placeholders})",
                tuple(vals),
            )

            update_sets = [
                "pickup_confirmed_phones=%s",
                "pickup_at=%s",
                "pickup_printed_name=%s",
                "pickup_signature=%s",
                "pickup_note=%s",
            ]
            update_vals: list[Any] = [new_total, now, printed_name, sig_bytes, note or None]

            if has_parent_chargers:
                exp_row = db.fetch_one(
                    "SELECT pickup_confirmed_chargers FROM dbo.event_exhibitors WHERE event_exhibitor_id=%s",
                    (event_exhibitor_id,),
                )
                prev_pick_ch = int((exp_row or {}).get("pickup_confirmed_chargers") or 0)
                update_sets.append("pickup_confirmed_chargers=%s")
                update_vals.append(prev_pick_ch + confirmed_chargers)

            update_vals.append(event_exhibitor_id)
            cur.execute(
                f"UPDATE dbo.event_exhibitors SET {', '.join(update_sets)} WHERE event_exhibitor_id=%s",
                tuple(update_vals),
            )
            if cur.rowcount <= 0:
                raise HTTPException(status_code=404, detail="Record not found")

    return {"ok": True, "pickup_at": now.isoformat()}


@app.get("/api/events/{event_id}/report")
def event_report(
    event_id: int,
    format: str = "json",
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
):
    _require_event_token(event_id, x_event_token)


def _phone_ids_to_csv(val: Any) -> str:
    txt = str(val or "").strip()
    if not txt:
        return ""
    import re

    parts = [t.strip() for t in re.split(r"[\r\n,;]+", txt) if t and t.strip()]
    return ", ".join(parts)

    if format.lower() == "csv":
        # One line per signed action (sign out or sign in). This preserves partial actions.
        # Phone IDs are stored on dropoff actions and (as a snapshot) on the parent row.
        has_action_phone_ids = _db_has_column("dbo.event_exhibitor_actions", "phone_ids")
        has_parent_phone_ids = _db_has_column("dbo.event_exhibitors", "dropoff_phone_ids")
        if has_action_phone_ids and has_parent_phone_ids:
            phone_ids_expr = "COALESCE(NULLIF(LTRIM(RTRIM(a.phone_ids)), ''), ee.dropoff_phone_ids) AS phone_ids"
        elif has_action_phone_ids:
            phone_ids_expr = "a.phone_ids AS phone_ids"
        elif has_parent_phone_ids:
            phone_ids_expr = "ee.dropoff_phone_ids AS phone_ids"
        else:
            phone_ids_expr = "CAST(NULL AS nvarchar(max)) AS phone_ids"

        rows = db.fetch_all(
            f"""
            SELECT
                a.action_id,
                ev.name AS event_name,
                e.display_name AS exhibitor_name,
                e.booth AS booth,
                ee.reserved_phones,
                a.action_type,
                a.quantity,
                a.action_at,
                a.printed_name,
                a.note,
                {phone_ids_expr}
            FROM dbo.event_exhibitor_actions a
            JOIN dbo.event_exhibitors ee ON ee.event_exhibitor_id = a.event_exhibitor_id
            JOIN dbo.events ev ON ev.event_id = ee.event_id
            JOIN dbo.exhibitors e ON e.exhibitor_id = ee.exhibitor_id
            WHERE ee.event_id = %s
            ORDER BY e.display_name ASC, a.action_at ASC, a.action_id ASC
            """,
            (event_id,),
        )

        # minimal CSV, no extra dependencies
        import csv
        import io

        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "action_id",
                "event_name",
                "exhibitor_name",
                "booth",
                "reserved_phones",
                "action_type",
                "quantity",
                "action_at",
                "printed_name",
                "note",
                "phone_ids",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        for r in rows:
            out = dict(r)
            out["action_type"] = "Signed Out" if r.get("action_type") == "dropoff" else "Signed In"
            out["phone_ids"] = _phone_ids_to_csv(r.get("phone_ids"))
            writer.writerow(out)
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")

    # One line per signed action (sign out or sign in). This preserves partial actions.
    rows = db.fetch_all(
        """
        SELECT
            a.action_id,
            ev.name AS event_name,
            e.display_name AS exhibitor_name,
            e.booth AS booth,
            ee.reserved_phones,
            a.action_type,
            a.quantity,
            a.action_at,
            a.printed_name,
            a.note
        FROM dbo.event_exhibitor_actions a
        JOIN dbo.event_exhibitors ee ON ee.event_exhibitor_id = a.event_exhibitor_id
        JOIN dbo.events ev ON ev.event_id = ee.event_id
        JOIN dbo.exhibitors e ON e.exhibitor_id = ee.exhibitor_id
        WHERE ee.event_id = %s
        ORDER BY e.display_name ASC, a.action_at ASC, a.action_id ASC
        """,
        (event_id,),
    )

    # Return plain data so FastAPI can JSON-encode datetimes safely.
    return rows


@app.get("/api/events/{event_id}/overview")
def event_overview_report(
    event_id: int,
    format: str = "json",
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
):
    _require_event_token(event_id, x_event_token)

    if format.lower() == "csv":
        has_parent_phone_ids = _db_has_column("dbo.event_exhibitors", "dropoff_phone_ids")
        phone_ids_expr = (
            "ee.dropoff_phone_ids AS phone_ids" if has_parent_phone_ids else "CAST(NULL AS nvarchar(max)) AS phone_ids"
        )
        rows = db.fetch_all(
            f"""
            SELECT
                ev.name AS event_name,
                e.display_name AS exhibitor_name,
                e.booth AS booth,
                ee.reserved_phones,
                ISNULL(ee.dropoff_confirmed_phones, 0) AS dropped_off_phones,
                ISNULL(ee.pickup_confirmed_phones, 0) AS picked_up_phones,
                ee.dropoff_at,
                ee.pickup_at,
                {phone_ids_expr}
            FROM dbo.event_exhibitors ee
            JOIN dbo.events ev ON ev.event_id = ee.event_id
            JOIN dbo.exhibitors e ON e.exhibitor_id = ee.exhibitor_id
            WHERE ee.event_id = %s
            ORDER BY e.display_name ASC
            """,
            (event_id,),
        )

        import csv
        import io

        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "event_name",
                "exhibitor_name",
                "booth",
                "reserved_phones",
                "signed_out_phones",
                "signed_in_phones",
                "signed_out_at",
                "signed_in_at",
                "phone_ids",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "event_name": r.get("event_name"),
                    "exhibitor_name": r.get("exhibitor_name"),
                    "booth": r.get("booth"),
                    "reserved_phones": r.get("reserved_phones"),
                    "signed_out_phones": r.get("dropped_off_phones"),
                    "signed_in_phones": r.get("picked_up_phones"),
                    "signed_out_at": r.get("dropoff_at"),
                    "signed_in_at": r.get("pickup_at"),
                    "phone_ids": _phone_ids_to_csv(r.get("phone_ids")),
                }
            )
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")

    rows = db.fetch_all(
        """
        SELECT
            ev.name AS event_name,
            e.display_name AS exhibitor_name,
            e.booth AS booth,
            ee.reserved_phones,
            ISNULL(ee.dropoff_confirmed_phones, 0) AS dropped_off_phones,
            ISNULL(ee.pickup_confirmed_phones, 0) AS picked_up_phones,
            ee.dropoff_at,
            ee.pickup_at
        FROM dbo.event_exhibitors ee
        JOIN dbo.events ev ON ev.event_id = ee.event_id
        JOIN dbo.exhibitors e ON e.exhibitor_id = ee.exhibitor_id
        WHERE ee.event_id = %s
        ORDER BY e.display_name ASC
        """,
        (event_id,),
    )

    return rows


@app.get("/api/event-exhibitor-actions/{action_id}/signature")
def get_action_signature(
    action_id: int,
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
):
    row = db.fetch_one(
        """
        SELECT ee.event_id, a.signature
        FROM dbo.event_exhibitor_actions a
        JOIN dbo.event_exhibitors ee ON ee.event_exhibitor_id = a.event_exhibitor_id
        WHERE a.action_id=%s
        """,
        (action_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Record not found")
    _require_event_token(int(row["event_id"]), x_event_token)
    sig = row.get("signature")
    if sig is None:
        raise HTTPException(status_code=404, detail="Signature not found")
    return Response(content=bytes(sig), media_type="image/png")


def _validate_running_total_note(actions: list[dict[str, Any]], expected: int, label: str) -> None:
    running = 0
    for a in actions:
        running += int(a.get("quantity") or 0)
        if running > expected:
            note = str(a.get("note") or "").strip()
            if not note:
                when = a.get("action_at")
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Discrepancy: {label} expected max {expected}, but running total reaches {running}. "
                        f"A note is required on the transaction that exceeds the expected count" + (f" (at {when})." if when else ".")
                    ),
                )


def _ensure_dict_rows(cur: Any, rows: list[Any]) -> list[dict[str, Any]]:
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return rows  # type: ignore[return-value]

    # pymssql tuple cursor: use cursor.description for column names.
    desc = getattr(cur, "description", None)
    if not desc:
        raise RuntimeError("DB cursor did not provide description for tuple rows")
    cols = [d[0] for d in desc]
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({cols[i]: r[i] for i in range(min(len(cols), len(r)))})
    return out


@app.patch("/api/event-exhibitor-actions/{action_id}")
def edit_event_exhibitor_action(
    action_id: int,
    payload: dict[str, Any],
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
) -> dict[str, Any]:
    # Editable fields: quantity, printed_name, note, phone_ids (dropoff), charger qtys.
    confirmed = payload.get("confirmed_phones")
    printed_name = str(payload.get("printed_name") or "").strip()
    note = str(payload.get("note") or "").strip()

    phone_ids = str(payload.get("phone_ids") or "").strip()
    charger_included = bool(payload.get("charger_included") or False)
    charger_qty_raw = payload.get("charger_qty")
    confirmed_chargers_raw = payload.get("confirmed_chargers")

    try:
        confirmed_int = int(confirmed)
    except Exception:
        raise HTTPException(status_code=400, detail="confirmed_phones must be an integer")

    if confirmed_int < 0:
        raise HTTPException(status_code=400, detail="confirmed_phones must be >= 0")
    if not printed_name:
        raise HTTPException(status_code=400, detail="Printed name is required")

    has_action_phone_ids = _db_has_column("dbo.event_exhibitor_actions", "phone_ids")
    has_action_charger_qty = _db_has_column("dbo.event_exhibitor_actions", "charger_qty")
    has_parent_phone_ids = _db_has_column("dbo.event_exhibitors", "dropoff_phone_ids")
    has_parent_drop_chargers = _db_has_column("dbo.event_exhibitors", "dropoff_confirmed_chargers")
    has_parent_pick_chargers = _db_has_column("dbo.event_exhibitors", "pickup_confirmed_chargers")

    # Fetch the existing action and associated exhibitor/event for auth and recompute.
    action_row = db.fetch_one(
        """
        SELECT
            a.action_id,
            a.event_exhibitor_id,
            a.action_type,
            a.action_at,
            ee.event_id,
            ee.reserved_phones
        FROM dbo.event_exhibitor_actions a
        JOIN dbo.event_exhibitors ee ON ee.event_exhibitor_id = a.event_exhibitor_id
        WHERE a.action_id=%s
        """,
        (action_id,),
    )
    if not action_row:
        raise HTTPException(status_code=404, detail="Record not found")
    event_id = int(action_row["event_id"])
    event_exhibitor_id = int(action_row["event_exhibitor_id"])
    action_type = str(action_row["action_type"] or "").strip().lower()
    reserved = int(action_row["reserved_phones"])
    _require_event_token(event_id, x_event_token)

    if action_type not in {"dropoff", "pickup"}:
        raise HTTPException(status_code=400, detail="Unsupported action type")

    # Validate phone IDs for dropoff edits (when feature is available).
    if action_type == "dropoff" and confirmed_int > 0:
        if not phone_ids:
            raise HTTPException(status_code=400, detail="Phone ID numbers are required when dropping off phones")
        import re

        parsed_ids = [t.strip() for t in re.split(r"[\r\n,;]+", phone_ids) if t and t.strip()]
        if len(parsed_ids) != confirmed_int:
            raise HTTPException(
                status_code=400,
                detail=f"Please provide exactly {confirmed_int} phone ID number(s). Got {len(parsed_ids)}.",
            )
        if not has_parent_phone_ids:
            raise HTTPException(
                status_code=409,
                detail="Database is missing dropoff_phone_ids column. Please run database/setup.sql schema upgrades.",
            )
        if not has_action_phone_ids:
            raise HTTPException(
                status_code=409,
                detail="Database is missing event_exhibitor_actions.phone_ids column. Please run database/setup.sql schema upgrades.",
            )

    # Validate charger quantities.
    dropoff_charger_qty: int | None = None
    pickup_charger_qty: int | None = None

    if action_type == "dropoff":
        if charger_included:
            if not has_action_charger_qty:
                raise HTTPException(
                    status_code=409,
                    detail="Database is missing event_exhibitor_actions.charger_qty column. Please run database/setup.sql schema upgrades.",
                )
            try:
                q = int(charger_qty_raw)
            except Exception:
                raise HTTPException(status_code=400, detail="charger_qty must be an integer")
            if q <= 0:
                raise HTTPException(status_code=400, detail="charger_qty must be >= 1 when charger_included is true")
            dropoff_charger_qty = q
        else:
            dropoff_charger_qty = None
    else:
        if confirmed_chargers_raw is not None:
            if not has_action_charger_qty:
                raise HTTPException(
                    status_code=409,
                    detail="Database is missing event_exhibitor_actions.charger_qty column. Please run database/setup.sql schema upgrades.",
                )
            try:
                q = int(confirmed_chargers_raw)
            except Exception:
                raise HTTPException(status_code=400, detail="confirmed_chargers must be an integer")
            if q < 0:
                raise HTTPException(status_code=400, detail="confirmed_chargers must be >= 0")
            pickup_charger_qty = q if q > 0 else None

    with db.get_conn() as conn:
        with conn.cursor(as_dict=True) as cur:
            # 1) Update the action row (do not change signature or timestamp).
            sets: list[str] = ["quantity=%s", "printed_name=%s", "note=%s"]
            vals: list[Any] = [confirmed_int, printed_name, note or None]

            if action_type == "dropoff" and has_action_phone_ids:
                sets.append("phone_ids=%s")
                vals.append(phone_ids or None)

            if has_action_charger_qty:
                if action_type == "dropoff":
                    sets.append("charger_qty=%s")
                    vals.append(dropoff_charger_qty)
                else:
                    # Only update when supplied; otherwise leave as-is.
                    if confirmed_chargers_raw is not None:
                        sets.append("charger_qty=%s")
                        vals.append(pickup_charger_qty)

            vals.append(action_id)
            cur.execute(
                f"UPDATE dbo.event_exhibitor_actions SET {', '.join(sets)} WHERE action_id=%s",
                tuple(vals),
            )
            if cur.rowcount <= 0:
                raise HTTPException(status_code=404, detail="Record not found")

            # 2) Recompute aggregates and snapshots from all actions.
            select_cols = [
                "a.action_id",
                "a.action_type",
                "a.quantity",
                "a.action_at",
                "a.printed_name",
                "a.signature",
                "a.note",
            ]
            if has_action_phone_ids:
                select_cols.append("a.phone_ids")
            if has_action_charger_qty:
                select_cols.append("a.charger_qty")

            cur.execute(
                f"""
                SELECT {', '.join(select_cols)}
                FROM dbo.event_exhibitor_actions a
                WHERE a.event_exhibitor_id=%s
                ORDER BY a.action_at ASC, a.action_id ASC
                """,
                (event_exhibitor_id,),
            )
            rows = _ensure_dict_rows(cur, list(cur.fetchall()))

            drop_actions = [r for r in rows if str(r.get("action_type") or "").lower() == "dropoff"]
            pick_actions = [r for r in rows if str(r.get("action_type") or "").lower() == "pickup"]

            drop_total = sum(int(r.get("quantity") or 0) for r in drop_actions)
            pick_total = sum(int(r.get("quantity") or 0) for r in pick_actions)

            # Expected counts depend on dropoff totals.
            expected_drop = reserved
            expected_pick = drop_total if drop_actions else reserved

            _validate_running_total_note(drop_actions, expected_drop, "sign out")
            _validate_running_total_note(pick_actions, expected_pick, "sign in")

            # Charger mismatch rule mirrors phone mismatch rule.
            if has_action_charger_qty and (has_parent_drop_chargers or has_parent_pick_chargers):
                expected_ch = sum(int(r.get("charger_qty") or 0) for r in drop_actions)
                running = 0
                for a in pick_actions:
                    running += int(a.get("charger_qty") or 0)
                    if running > expected_ch:
                        n = str(a.get("note") or "").strip()
                        if not n:
                            when = a.get("action_at")
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f"Discrepancy: charger sign in expected max {expected_ch}, but running total reaches {running}. "
                                    f"A note is required on the transaction that exceeds the expected count" + (f" (at {when})." if when else ".")
                                ),
                            )

            # Determine last snapshots (most recent by action_at, action_id).
            def _last(xs: list[dict[str, Any]]) -> dict[str, Any] | None:
                if not xs:
                    return None
                return sorted(xs, key=lambda a: (a.get("action_at") or _utc_now(), int(a.get("action_id") or 0)))[-1]

            last_drop = _last(drop_actions)
            last_pick = _last(pick_actions)

            update_sets: list[str] = []
            update_vals2: list[Any] = []

            update_sets.append("dropoff_confirmed_phones=%s")
            update_vals2.append(drop_total if drop_total > 0 else None)
            update_sets.append("pickup_confirmed_phones=%s")
            update_vals2.append(pick_total if pick_total > 0 else None)

            if last_drop:
                update_sets.extend(
                    [
                        "dropoff_at=%s",
                        "dropoff_printed_name=%s",
                        "dropoff_signature=%s",
                        "dropoff_note=%s",
                    ]
                )
                update_vals2.extend(
                    [
                        last_drop.get("action_at"),
                        last_drop.get("printed_name"),
                        last_drop.get("signature"),
                        last_drop.get("note"),
                    ]
                )
            else:
                update_sets.extend(["dropoff_at=%s", "dropoff_printed_name=%s", "dropoff_signature=%s", "dropoff_note=%s"])
                update_vals2.extend([None, None, None, None])

            if last_pick:
                update_sets.extend(
                    [
                        "pickup_at=%s",
                        "pickup_printed_name=%s",
                        "pickup_signature=%s",
                        "pickup_note=%s",
                    ]
                )
                update_vals2.extend(
                    [
                        last_pick.get("action_at"),
                        last_pick.get("printed_name"),
                        last_pick.get("signature"),
                        last_pick.get("note"),
                    ]
                )
            else:
                update_sets.extend(["pickup_at=%s", "pickup_printed_name=%s", "pickup_signature=%s", "pickup_note=%s"])
                update_vals2.extend([None, None, None, None])

            if has_parent_phone_ids and has_action_phone_ids:
                combined_ids = "\n".join(
                    [str(r.get("phone_ids") or "").strip() for r in drop_actions if str(r.get("phone_ids") or "").strip()]
                ).strip()
                update_sets.append("dropoff_phone_ids=%s")
                update_vals2.append(combined_ids or None)

            if has_parent_drop_chargers and has_action_charger_qty:
                update_sets.append("dropoff_confirmed_chargers=%s")
                update_vals2.append(sum(int(r.get("charger_qty") or 0) for r in drop_actions) or None)

            if has_parent_pick_chargers and has_action_charger_qty:
                update_sets.append("pickup_confirmed_chargers=%s")
                update_vals2.append(sum(int(r.get("charger_qty") or 0) for r in pick_actions) or None)

            update_vals2.append(event_exhibitor_id)
            cur.execute(
                f"UPDATE dbo.event_exhibitors SET {', '.join(update_sets)} WHERE event_exhibitor_id=%s",
                tuple(update_vals2),
            )
            if cur.rowcount <= 0:
                raise HTTPException(status_code=404, detail="Record not found")

    return {"ok": True}


@app.get("/api/event-exhibitors/{event_exhibitor_id}/actions")
def list_event_exhibitor_actions(
    event_exhibitor_id: int,
    x_event_token: str | None = Header(default=None, alias="X-Event-Token"),
) -> list[dict[str, Any]]:
    row = db.fetch_one(
        "SELECT event_id FROM dbo.event_exhibitors WHERE event_exhibitor_id=%s",
        (event_exhibitor_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Record not found")
    event_id = int(row["event_id"])
    _require_event_token(event_id, x_event_token)

    has_action_phone_ids = _db_has_column("dbo.event_exhibitor_actions", "phone_ids")
    has_action_charger_qty = _db_has_column("dbo.event_exhibitor_actions", "charger_qty")

    extra_cols: list[str] = []
    if has_action_phone_ids:
        extra_cols.append("a.phone_ids")
    if has_action_charger_qty:
        extra_cols.append("a.charger_qty")

    extra_sql = (",\n            " + ",\n            ".join(extra_cols)) if extra_cols else ""

    rows = db.fetch_all(
        f"""
        SELECT
            a.action_id,
            a.action_type,
            a.quantity,
            a.action_at,
            a.printed_name,
            a.note,
            CASE WHEN a.signature IS NULL THEN CAST(0 AS bit) ELSE CAST(1 AS bit) END AS has_signature
            {extra_sql}
        FROM dbo.event_exhibitor_actions a
        WHERE a.event_exhibitor_id=%s
        ORDER BY a.action_at ASC, a.action_id ASC
        """,
        (event_exhibitor_id,),
    )

    for r in rows:
        action_id = r.get("action_id")
        r["signature_url"] = f"/api/event-exhibitor-actions/{action_id}/signature" if (action_id and r.get("has_signature")) else None
    return rows


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}
