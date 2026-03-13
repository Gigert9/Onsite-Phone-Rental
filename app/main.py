from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .excel_import import parse_totali_phone_rentals_xls


APP_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = APP_ROOT / "static"

app = FastAPI(title="Onsite Leads Phone Dropoff")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


PBKDF2_ITERATIONS = 210_000
PBKDF2_HASH_NAME = "sha256"

# In-memory unlock tokens: { (event_id, token): expires_epoch }
_EVENT_TOKENS: dict[tuple[int, str], float] = {}


def _clean_expired_tokens(now: float | None = None) -> None:
    ts = now if now is not None else time.time()
    expired = [k for k, exp in _EVENT_TOKENS.items() if exp <= ts]
    for k in expired:
        _EVENT_TOKENS.pop(k, None)


def _issue_event_token(event_id: int, hours: int = 12) -> str:
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
        with conn.cursor() as cur:
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
    rows = db.fetch_all(
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
        WHERE ee.event_id = %s
        ORDER BY e.display_name ASC
        """,
        (event_id,),
    )
    return rows


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

    try:
        confirmed_int = int(confirmed)
    except Exception:
        raise HTTPException(status_code=400, detail="confirmed_phones must be an integer")

    if confirmed_int < 0:
        raise HTTPException(status_code=400, detail="confirmed_phones must be >= 0")
    if not printed_name:
        raise HTTPException(status_code=400, detail="Printed name is required")

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
                f"Discrepancy: reserved {expected}, already dropped off {prev_total}, "
                f"dropping off {confirmed_int} (new total {new_total}). Note is required to continue."
            ),
        )

    try:
        sig_bytes = _decode_data_url_png(signature)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    now = _utc_now()

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # Insert signed action record
            cur.execute(
                """
                INSERT INTO dbo.event_exhibitor_actions
                    (event_exhibitor_id, action_type, quantity, action_at, printed_name, signature, note)
                VALUES (%s, N'dropoff', %s, %s, %s, %s, %s)
                """,
                (event_exhibitor_id, confirmed_int, now, printed_name, sig_bytes, note or None),
            )

            # Update fast-path totals + keep last-action snapshot on the parent row.
            cur.execute(
                """
                UPDATE dbo.event_exhibitors
                SET dropoff_confirmed_phones=%s,
                    dropoff_at=%s,
                    dropoff_printed_name=%s,
                    dropoff_signature=%s,
                    dropoff_note=%s
                WHERE event_exhibitor_id=%s
                """,
                (new_total, now, printed_name, sig_bytes, note or None, event_exhibitor_id),
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

    try:
        confirmed_int = int(confirmed)
    except Exception:
        raise HTTPException(status_code=400, detail="confirmed_phones must be an integer")

    if confirmed_int < 0:
        raise HTTPException(status_code=400, detail="confirmed_phones must be >= 0")
    if not printed_name:
        raise HTTPException(status_code=400, detail="Printed name is required")

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
                f"Discrepancy: expected pick-up {expected}, already picked up {prev_total}, "
                f"picking up {confirmed_int} (new total {new_total}). Note is required to continue."
            ),
        )

    try:
        sig_bytes = _decode_data_url_png(signature)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    now = _utc_now()

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dbo.event_exhibitor_actions
                    (event_exhibitor_id, action_type, quantity, action_at, printed_name, signature, note)
                VALUES (%s, N'pickup', %s, %s, %s, %s, %s)
                """,
                (event_exhibitor_id, confirmed_int, now, printed_name, sig_bytes, note or None),
            )

            cur.execute(
                """
                UPDATE dbo.event_exhibitors
                SET pickup_confirmed_phones=%s,
                    pickup_at=%s,
                    pickup_printed_name=%s,
                    pickup_signature=%s,
                    pickup_note=%s
                WHERE event_exhibitor_id=%s
                """,
                (new_total, now, printed_name, sig_bytes, note or None, event_exhibitor_id),
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
    # One line per signed action (drop-off or pick-up). This preserves partial actions.
    rows = db.fetch_all(
        """
        SELECT
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

    if format.lower() == "csv":
        # minimal CSV, no extra dependencies
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
                "action_type",
                "quantity",
                "action_at",
                "printed_name",
                "note",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")

    # Return plain data so FastAPI can JSON-encode datetimes safely.
    return rows


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}
