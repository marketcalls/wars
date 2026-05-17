"""
Web-app integration sketch.

Demonstrates the **recommended persistence pattern**: keep the WhatsApp
session as bytes inside your existing application database (here we
default to ``app.db``, but it could be Postgres / Redis / a secret
manager — anywhere you already store API keys). No separate
``whatsapp.db`` file to back up or chmod.

Flow:
  1. First run: pair via QR/pair-code in a short-lived temp DB,
     ``wa.export_session()`` → stash bytes in ``app.db``, temp file is
     deleted.
  2. Every subsequent run: load bytes from ``app.db`` →
     ``WhatsApp.from_bytes(blob)`` → instant reconnect, no QR.

Pair the device interactively the first time::

    python examples/webapp_integration.py pair --phone 14155550100

Then send from your Flask app::

    from webapp_integration import notify
    notify("Build #4321 finished")
"""

from __future__ import annotations

import argparse
import atexit
import os
import sqlite3
import sys
from threading import Lock

from wars import WhatsApp


# ─── Config ─────────────────────────────────────────────────────────
APP_DB = os.environ.get("WARS_DB", "app.db")
# Default recipient comes from the paired device — no env var needed.
# For sends to a different number, pass `to=` to notify()/wa().send().


# ─── DB helpers (one tiny table beside your other tables) ───────────
def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wars_session "
        "(id INTEGER PRIMARY KEY CHECK (id = 1), session BLOB NOT NULL, "
        "updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')))"
    )
    conn.commit()


def load_session_blob() -> bytes | None:
    with sqlite3.connect(APP_DB) as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT session FROM wars_session WHERE id = 1"
        ).fetchone()
    return row[0] if row else None


def save_session_blob(blob: bytes) -> None:
    with sqlite3.connect(APP_DB) as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO wars_session (id, session) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET session = excluded.session, "
            "updated_at = strftime('%s', 'now')",
            (blob,),
        )
        conn.commit()


# ─── Singleton — one connection per process ─────────────────────────
_wa: WhatsApp | None = None
_lock = Lock()


def wa() -> WhatsApp:
    """Lazy singleton — first call connects, the rest are free."""
    global _wa
    if _wa is not None:
        return _wa
    with _lock:
        if _wa is not None:
            return _wa
        blob = load_session_blob()
        if blob is None:
            raise RuntimeError(
                f"No paired session in {APP_DB} — run "
                "`python examples/webapp_integration.py pair --phone <YOURS>` "
                "once to pair."
            )
        client = WhatsApp.from_bytes(blob)
        client.connect()
        client.wait_until_ready(timeout=60)
        atexit.register(client.disconnect)
        _wa = client
        return _wa


# ─── Notification helpers — what your Flask handlers call ───────────
def notify(text: str, to: str | None = None) -> str:
    """Send a notification. Defaults to the device's own paired number."""
    return wa().send(to, text) if to else wa().send(text)


def notify_with_image(text: str, image_path: str, to: str | None = None) -> str:
    target = to or wa()._inner.own_phone()
    return wa().send(target, image=image_path, caption=text)


# ─── One-time pair: pair the device, then stash session in app.db ───
def _do_pair(phone: str | None) -> int:
    """
    Pair using a short-lived temp file (export_session() needs a
    file-backed DB — see export_session docstring). After exporting
    the bytes into app.db, we delete the temp file so nothing private
    is left on disk.
    """
    import tempfile

    print("Pairing — scan the QR shown in your terminal on your phone…")

    fd, pair_db = tempfile.mkstemp(suffix=".db", prefix="wars_pair_")
    os.close(fd)
    os.chmod(pair_db, 0o600)

    client = WhatsApp(pair_db, log_level="error")

    @client.on_qr
    def _qr(code):
        WhatsApp.print_qr(code)

    @client.on_pair_code
    def _pc(code):
        print(f"Pair code: {code}")

    client.connect(phone=phone)
    try:
        try:
            client.wait_until_ready(timeout=300)
        except TimeoutError:
            print("Pairing timed out.", file=sys.stderr)
            return 1
        blob = client.export_session()
        save_session_blob(blob)
        print(f"✔ Paired. Session ({len(blob)} bytes) saved into {APP_DB}")
        client.disconnect()
    finally:
        try:
            os.unlink(pair_db)
        except OSError:
            pass
    return 0


# ─── CLI entry ──────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="wars web-app integration helper")
    sub = p.add_subparsers(dest="cmd")
    p_pair = sub.add_parser("pair", help=f"Pair the device and stash session in {APP_DB}")
    p_pair.add_argument("--phone", help="E.164 digits, enables pair code in addition to QR")
    sub.add_parser("test", help="Send a test message to your own number")
    args = p.parse_args()

    if args.cmd == "pair":
        return _do_pair(args.phone)
    if args.cmd == "test":
        mid = notify("wars test ✓")
        print(f"sent: {mid}")
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


# ─── Flask usage ─────────────────────────────────────────────────────
#
# from flask import Flask, request
# app = Flask(__name__)
#
# @app.post("/webhook/build")
# def build_done():
#     data = request.json
#     notify(f"Build #{data['id']} {data['status']} in {data['duration']}s")
#     return "ok"
