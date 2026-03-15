# Onsite Leads Phone Sign In/Out

Tablet-friendly web app for tracking iPhone rental **sign out** and **sign in** per exhibitor at an event.

## What It Does

- Create events
- Import exhibitors + reserved phone counts from the Totali Rentals Excel report
- Capture a printed name + signature for every sign out and sign in (supports partial actions)
- Export a single CSV report (one row per action)
- Optional per-event password (required to open an event)
- Delete events (permanent; includes confirmation)

## Requirements

- Python 3.11+ (recommended)
- Microsoft SQL Server (with credentials created by the SQL script)

## Setup

### 1) Create the database + tables

- Run [database/setup.sql](database/setup.sql) in SSMS (as sysadmin or equivalent).

This script creates:
- Database `Onsite_Leads_Phone_Dropoff` (if missing)
- Login/user `phonerental`
- Tables: `events`, `exhibitors`, `event_exhibitors`, `event_exhibitor_actions`

If you already ran an earlier version of the script, re-running it is safe; it uses `IF OBJECT_ID(...) IS NULL` / `ALTER TABLE` guards.

### 2) Configure environment variables

- Copy `.env.example` to `.env` and update as needed.

Note: the app will only auto-load `.env` if `python-dotenv` is installed. If you don't want to install it, set env vars in PowerShell instead.

Required env vars:
- `MSSQL_SERVER`
- `MSSQL_PORT` (default `1433`)
- `MSSQL_DATABASE`
- `MSSQL_USER`
- `MSSQL_PASSWORD`

### 3) Install dependencies

- `.venv\Scripts\python.exe -m pip install -r requirements.txt`

### 4) Run

- `.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000`

Open `http://localhost:8000/`.

## Usage

1) Create an event
2) Set an event password when prompted (required to open the event)
3) Open the event
4) Import the Totali Rentals Excel report
5) For each exhibitor:
   - Use **Sign Out** / **Sign In** to record actions
   - Partial sign outs/sign ins are allowed (e.g., sign out 1 now, another later)
   - A note is required only when the cumulative total would exceed the expected count
6) Download the CSV report at any time

## Excel Import

The import expects the same columns as the provided sample report:

- `Exhibitor/Booth`
- `iPhones` (this is the phone rentals column)
- `Licenses` is imported for reference but is not used for phone confirmation

## CSV Report

The CSV export is **action-level** (one row per signed out / signed in action), so partial actions create multiple rows for an exhibitor.

Columns:

- `event_name`
- `exhibitor_name`
- `booth`
- `reserved_phones`
- `action_type` (`Signed Out` or `Signed In`)
- `quantity` (the number of phones recorded in that action)
- `action_at` (UTC)
- `printed_name`
- `note`

## SQL Server Connectivity Notes

If you see connection errors, confirm your SQL Server instance is listening on the configured host/port and update `MSSQL_SERVER`/`MSSQL_PORT` accordingly.
