"""Persistent, non-secret operational settings and their audit trail."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(os.getenv("RUNTIME_CONFIG_PATH", Path(__file__).parent / "runtime_config.db"))

DEFAULTS = {
    "OPEN_ROUTER_MODEL": os.getenv("OPEN_ROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
    "SOURCE_CHANNEL_ID": os.getenv("SOURCE_CHANNEL_ID", ""),
    "SOURCE_CHANNEL2_ID": os.getenv("SOURCE_CHANNEL2_ID", ""),
    "TARGET_CHANNEL_ID": os.getenv("TARGET_CHANNEL_ID", ""),
    "NOTIF_CHANNEL_ID": os.getenv("NOTIF_CHANNEL_ID", ""),
    "PRICE_PREDICTIONS_ENABLED": os.getenv("PRICE_PREDICTIONS_ENABLED", "true"),
    "EPL_LEAGUE_CODE": os.getenv("EPL_LEAGUE_CODE", os.getenv("LEAGUE_CODE", "433b70")),
    "EPL_LEAGUE_ID": os.getenv("EPL_LEAGUE_ID", ""),
    "IRAN_LEAGUE_ID": os.getenv("IRAN_LEAGUE_ID", ""),
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                old_value TEXT NOT NULL,
                new_value TEXT NOT NULL,
                changed_at TEXT NOT NULL
            );
            """
        )
        now = datetime.now(timezone.utc).isoformat()
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )


def get(key: str) -> str:
    init()
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else DEFAULTS.get(key, "")


def get_bool(key: str) -> bool:
    return get(key).strip().lower() not in {"0", "false", "no", "off", ""}


def values() -> dict[str, str]:
    init()
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return dict(rows)


def set_value(key: str, value: str, actor_id: int) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"Unsupported runtime setting: {key}")
    value = value.strip()
    old_value = get(key)
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now),
        )
        conn.execute(
            "INSERT INTO audit_log (actor_id, key, old_value, new_value, changed_at) VALUES (?, ?, ?, ?, ?)",
            (actor_id, key, old_value, value, now),
        )


def recent_audit(limit: int = 8) -> list[dict]:
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT actor_id, key, old_value, new_value, changed_at FROM audit_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(zip(("actor_id", "key", "old_value", "new_value", "changed_at"), row)) for row in rows]
