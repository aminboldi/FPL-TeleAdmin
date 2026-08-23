"""Persistent, non-secret operational settings and their audit trail."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(os.getenv("RUNTIME_CONFIG_PATH", Path(__file__).parent / "runtime_config.db"))

DEFAULTS = {
    "OPEN_ROUTER_MODEL": os.getenv("OPEN_ROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
    "TARGET_CHANNEL_ID": os.getenv("TARGET_CHANNEL_ID", ""),
    "PRICE_PREDICTIONS_ENABLED": os.getenv("PRICE_PREDICTIONS_ENABLED", "true"),
    "EPL_LEAGUE_CODE": os.getenv("EPL_LEAGUE_CODE", os.getenv("LEAGUE_CODE", "433b70")),
    "EPL_LEAGUE_ID": os.getenv("EPL_LEAGUE_ID", ""),
    "IRAN_LEAGUE_ID": os.getenv("IRAN_LEAGUE_ID", ""),
    "ARTICLE_MONITOR_ENABLED": os.getenv("ARTICLE_MONITOR_ENABLED", "true"),
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
            CREATE TABLE IF NOT EXISTS youtube_channels (
                channel_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                uploads_playlist_id TEXT NOT NULL,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS youtube_seen_videos (
                video_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL REFERENCES youtube_channels(channel_id)
                    ON DELETE CASCADE,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_sources (
                channel_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                added_at TEXT NOT NULL,
                automatic_only INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS article_monitor_seen (
                url TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS article_monitor_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS youtube_transcript_provider_health (
                provider_key TEXT PRIMARY KEY,
                disabled_month TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            """
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(telegram_sources)").fetchall()
        }
        if "automatic_only" not in columns:
            conn.execute(
                "ALTER TABLE telegram_sources ADD COLUMN automatic_only INTEGER NOT NULL DEFAULT 0"
            )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "DELETE FROM settings WHERE key IN (?, ?, ?)",
            ("SOURCE_CHANNEL_ID", "SOURCE_CHANNEL2_ID", "NOTIF_CHANNEL_ID"),
        )
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


def article_monitor_is_bootstrapped() -> bool:
    init()
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM article_monitor_meta WHERE key='bootstrapped'"
        ).fetchone() is not None


def mark_article_monitor_bootstrapped() -> None:
    init()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO article_monitor_meta (key, value) VALUES ('bootstrapped', '1')"
        )


def seed_article_monitor_candidates(candidates: list[dict]) -> None:
    """Remember the current source contents without importing the backlog."""
    if not candidates:
        return
    init()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO article_monitor_seen "
            "(url, source_key, title, published_at, status, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'seeded', ?, ?)",
            [
                (
                    str(candidate["url"]),
                    str(candidate.get("source_key", "")),
                    str(candidate.get("title", "")),
                    str(candidate.get("published_at", "")),
                    now,
                    now,
                )
                for candidate in candidates
            ],
        )


def claim_article_monitor_candidate(
    candidate: dict, *, retry_after_seconds: int = 6 * 60 * 60
) -> bool:
    """Reserve a new or retryable candidate for one import attempt."""
    init()
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    url = str(candidate["url"])
    with _connect() as conn:
        row = conn.execute(
            "SELECT status, attempts, updated_at FROM article_monitor_seen WHERE url=?",
            (url,),
        ).fetchone()
        if row:
            status, attempts, updated_at = row
            if status in {"published", "seeded"}:
                return False
            try:
                age = (now - datetime.fromisoformat(updated_at)).total_seconds()
            except ValueError:
                age = retry_after_seconds
            if status == "processing" and age < 60 * 60:
                return False
            if status == "failed" and age < retry_after_seconds:
                return False
            conn.execute(
                "UPDATE article_monitor_seen SET status='processing', attempts=?, "
                "last_error='', updated_at=? WHERE url=?",
                (int(attempts or 0) + 1, now_text, url),
            )
            return True
        conn.execute(
            "INSERT INTO article_monitor_seen "
            "(url, source_key, title, published_at, status, attempts, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'processing', 1, ?, ?)",
            (
                url,
                str(candidate.get("source_key", "")),
                str(candidate.get("title", "")),
                str(candidate.get("published_at", "")),
                now_text,
                now_text,
            ),
        )
        return True


def finish_article_monitor_candidate(url: str, *, success: bool, error: str = "") -> None:
    init()
    with _connect() as conn:
        conn.execute(
            "UPDATE article_monitor_seen SET status=?, last_error=?, updated_at=? WHERE url=?",
            (
                "published" if success else "failed",
                str(error)[:1000],
                datetime.now(timezone.utc).isoformat(),
                url,
            ),
        )


def transcript_provider_is_disabled(provider_key: str, month: str) -> bool:
    init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT disabled_month FROM youtube_transcript_provider_health "
            "WHERE provider_key=?",
            (provider_key,),
        ).fetchone()
    return bool(row and row[0] == month)


def disable_transcript_provider(provider_key: str, month: str, error: str) -> None:
    init()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO youtube_transcript_provider_health "
            "(provider_key, disabled_month, last_error, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider_key) DO UPDATE SET disabled_month=excluded.disabled_month, "
            "last_error=excluded.last_error, updated_at=excluded.updated_at",
            (
                provider_key,
                month,
                str(error)[:1000],
                datetime.now(timezone.utc).isoformat(),
            ),
        )


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


def youtube_channels() -> list[dict]:
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT channel_id, title, uploads_playlist_id, added_at "
            "FROM youtube_channels ORDER BY title COLLATE NOCASE"
        ).fetchall()
    return [
        dict(zip(("channel_id", "title", "uploads_playlist_id", "added_at"), row))
        for row in rows
    ]


def add_youtube_channel(
    channel_id: str, title: str, uploads_playlist_id: str, actor_id: int
) -> bool:
    """Persist a monitored channel. Returns whether it was newly added."""
    init()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT title FROM youtube_channels WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO youtube_channels "
            "(channel_id, title, uploads_playlist_id, added_at) VALUES (?, ?, ?, ?)",
            (channel_id, title, uploads_playlist_id, now),
        )
        conn.execute(
            "INSERT INTO audit_log (actor_id, key, old_value, new_value, changed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor_id, "YOUTUBE_CHANNEL", "", f"added: {title} ({channel_id})", now),
        )
    return True


def remove_youtube_channel(channel_id: str, actor_id: int) -> str | None:
    """Remove a monitored channel and its remembered videos."""
    init()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT title FROM youtube_channels WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if not row:
            return None
        title = row[0]
        conn.execute("DELETE FROM youtube_seen_videos WHERE channel_id=?", (channel_id,))
        conn.execute("DELETE FROM youtube_channels WHERE channel_id=?", (channel_id,))
        conn.execute(
            "INSERT INTO audit_log (actor_id, key, old_value, new_value, changed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor_id, "YOUTUBE_CHANNEL", f"removed: {title} ({channel_id})", "", now),
        )
    return title


def youtube_video_seen(video_id: str) -> bool:
    init()
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM youtube_seen_videos WHERE video_id=?", (video_id,)
        ).fetchone() is not None


def mark_youtube_video(video_id: str, channel_id: str, state: str) -> None:
    init()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO youtube_seen_videos (video_id, channel_id, state, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(video_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (video_id, channel_id, state, now),
        )


def telegram_sources() -> list[dict]:
    """Return the persisted Telegram channels that feed the translation flow."""
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT channel_id, title, source_ref, added_at, automatic_only "
            "FROM telegram_sources ORDER BY title COLLATE NOCASE"
        ).fetchall()
    return [
        dict(zip(("channel_id", "title", "source_ref", "added_at", "automatic_only"), row))
        for row in rows
    ]


def is_telegram_source(channel_id: int) -> bool:
    init()
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM telegram_sources WHERE channel_id=?", (channel_id,)
        ).fetchone() is not None


def telegram_source_is_automatic_only(channel_id: int) -> bool:
    init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT automatic_only FROM telegram_sources WHERE channel_id=?",
            (channel_id,),
        ).fetchone()
    return bool(row and row[0])


def telegram_source_by_reference(reference: str) -> dict | None:
    """Look up a source by its saved @username or numeric channel ID."""
    init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT channel_id, title, source_ref, added_at, automatic_only FROM telegram_sources "
            "WHERE source_ref=? OR CAST(channel_id AS TEXT)=?",
            (reference, reference.lstrip("-")),
        ).fetchone()
    if not row:
        return None
    return dict(zip(("channel_id", "title", "source_ref", "added_at", "automatic_only"), row))


def add_telegram_source(
    channel_id: int, title: str, source_ref: str, actor_id: int,
    *, automatic_only: bool = False,
) -> bool:
    """Persist a source channel. Returns whether it was newly added."""
    init()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT title, automatic_only FROM telegram_sources WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if existing:
            if automatic_only and not existing[1]:
                conn.execute(
                    "UPDATE telegram_sources SET automatic_only=1 WHERE channel_id=?",
                    (channel_id,),
                )
                conn.execute(
                    "INSERT INTO audit_log (actor_id, key, old_value, new_value, changed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (actor_id, "TELEGRAM_SOURCE", f"updated: {existing[0]}", "automatic_only", now),
                )
            return False
        conn.execute(
            "INSERT INTO telegram_sources "
            "(channel_id, title, source_ref, added_at, automatic_only) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, title, source_ref, now, int(automatic_only)),
        )
        conn.execute(
            "INSERT INTO audit_log (actor_id, key, old_value, new_value, changed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                actor_id,
                "TELEGRAM_SOURCE",
                "",
                f"added: {title} ({source_ref})"
                + (" [automatic-only]" if automatic_only else ""),
                now,
            ),
        )
    return True


def remove_telegram_source(channel_id: int, actor_id: int) -> str | None:
    """Remove a source channel and return its title when it existed."""
    init()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT title, source_ref FROM telegram_sources WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if not row:
            return None
        title, source_ref = row
        conn.execute("DELETE FROM telegram_sources WHERE channel_id=?", (channel_id,))
        conn.execute(
            "INSERT INTO audit_log (actor_id, key, old_value, new_value, changed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor_id, "TELEGRAM_SOURCE", f"removed: {title} ({source_ref})", "", now),
        )
    return title
