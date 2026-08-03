"""Persistent local index for Telegraph articles published by TeleAdmin."""
import html
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import runtime_config


def _connect() -> sqlite3.Connection:
    runtime_config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(runtime_config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegraph_articles (
                path TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                source_tag TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegraph_articles_published "
            "ON telegraph_articles(published_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegraph_articles_source "
            "ON telegraph_articles(source_tag)"
        )


def _path_from_url(url: str) -> str:
    value = str(url or "").strip().rstrip("/")
    return value.rsplit("/", 1)[-1]


def _plain_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def record_page(
    url: str,
    title: str,
    summary: str = "",
    source_tag: str = "",
    image_url: str = "",
    published_at: str | None = None,
) -> None:
    """Insert or update a locally indexed Telegraph page."""
    path = _path_from_url(url)
    if not path:
        return
    now = datetime.now(timezone.utc).isoformat()
    published_at = published_at or now
    init()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO telegraph_articles
                (path, url, title, summary, source_tag, image_url,
                 published_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                url=excluded.url,
                title=excluded.title,
                summary=excluded.summary,
                source_tag=CASE
                    WHEN telegraph_articles.source_tag <> ''
                    THEN telegraph_articles.source_tag
                    ELSE excluded.source_tag
                END,
                image_url=CASE
                    WHEN excluded.image_url <> '' THEN excluded.image_url
                    ELSE telegraph_articles.image_url
                END,
                updated_at=excluded.updated_at
            """,
            (
                path,
                url,
                _plain_text(title),
                _plain_text(summary),
                _plain_text(source_tag),
                str(image_url or "").strip(),
                published_at,
                now,
            ),
        )


def list_source_tags() -> list[str]:
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT source_tag FROM telegraph_articles "
            "WHERE source_tag <> '' ORDER BY source_tag COLLATE NOCASE"
        ).fetchall()
    return [str(row[0]) for row in rows]


def list_pages(
    *, query: str = "", source_tag: str = "", limit: int = 24, offset: int = 0,
) -> list[dict]:
    init()
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    clauses = []
    params: list[str | int] = []
    if query.strip():
        pattern = f"%{query.strip()}%"
        clauses.append("(title LIKE ? OR summary LIKE ? OR source_tag LIKE ?)")
        params.extend((pattern, pattern, pattern))
    if source_tag.strip():
        clauses.append("source_tag = ?")
        params.append(source_tag.strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT path, url, title, summary, source_tag, image_url, "
            "published_at, updated_at "
            f"FROM telegraph_articles {where} "
            "ORDER BY published_at DESC, path DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def has_more(
    *, query: str = "", source_tag: str = "", offset: int = 0, limit: int = 24,
) -> bool:
    return len(list_pages(
        query=query, source_tag=source_tag, limit=limit + 1, offset=offset,
    )) > limit


def sync_from_telegraph() -> int:
    """Import all pages visible through the configured Telegraph account."""
    import articles

    if not os.getenv("TELEGRAPH_ACCESS_TOKEN"):
        return 0
    imported = 0
    offset = 0
    limit = 200
    while True:
        pages = articles.get_telegraph_page_list(limit=limit, offset=offset)
        if not pages:
            break
        for page_index, page in enumerate(pages):
            imported_at = (
                datetime.now(timezone.utc)
                - timedelta(seconds=offset + page_index)
            ).isoformat()
            record_page(
                page.get("url", ""),
                page.get("title", ""),
                page.get("description", ""),
                "آرشیو",
                page.get("image_url", ""),
                published_at=imported_at,
            )
            imported += 1
        if len(pages) < limit:
            break
        offset += limit
    return imported
