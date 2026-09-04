"""Persistent local index for Telegraph articles published by TeleAdmin."""
import html
import os
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import runtime_config

_TELEGRAPH_HOSTS = {"telegra.ph", "graph.org"}
# Tracking parameters are added by whoever shares a link, not by the article,
# so two URLs that differ only by these point at the same source article.
_TRACKING_QUERY_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref",
    "source", "amp", "at_medium", "at_campaign",
}
_YOUTUBE_HOSTS = {
    "youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
}
_YOUTUBE_PATH_ID_RE = re.compile(r"^/(?:shorts|embed|live|v)/([^/?#]+)")
_PL_NEWS_PATH_RE = re.compile(r"^/(?:en/)?news/(\d+)")
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)
_SOURCE_LINK_LABEL = "منبع اصلی"


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
                summary_source TEXT NOT NULL DEFAULT 'ai',
                source_tag TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                hidden INTEGER NOT NULL DEFAULT 0,
                published_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS telegraph_catalog_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(telegraph_articles)")
        }
        if "summary_source" not in columns:
            conn.execute(
                "ALTER TABLE telegraph_articles ADD COLUMN "
                "summary_source TEXT NOT NULL DEFAULT 'ai'"
            )
        if "source_url" not in columns:
            conn.execute(
                "ALTER TABLE telegraph_articles ADD COLUMN "
                "source_url TEXT NOT NULL DEFAULT ''"
            )
        if "hidden" not in columns:
            conn.execute(
                "ALTER TABLE telegraph_articles ADD COLUMN "
                "hidden INTEGER NOT NULL DEFAULT 0"
            )
        if "enriched_at" not in columns:
            # Enrichment order needs its own timestamp. ``updated_at`` cannot
            # serve: importing the Telegraph account rewrites it for every
            # page, which would keep resetting the backlog position.
            conn.execute(
                "ALTER TABLE telegraph_articles ADD COLUMN "
                "enriched_at TEXT NOT NULL DEFAULT ''"
            )
        cleared = conn.execute(
            "SELECT 1 FROM telegraph_catalog_meta WHERE key='catalog_footer_source_cleared'"
        ).fetchone()
        catalogue_url = "" if cleared else _catalogue_url()
        if catalogue_url:
            # ``first_source_url`` used to accept any non-Telegraph link, so it
            # recovered the catalogue footer that every published page ends
            # with instead of the original article. Those values are not source
            # articles, and keeping them would block re-recovery.
            conn.execute(
                "UPDATE telegraph_articles SET source_url='' WHERE source_url LIKE ?",
                (f"{catalogue_url}%",),
            )
            conn.execute(
                "INSERT INTO telegraph_catalog_meta (key, value) "
                "VALUES ('catalog_footer_source_cleared', '1')"
            )
        migrated = conn.execute(
            "SELECT 1 FROM telegraph_catalog_meta WHERE key='summary_source_migrated'"
        ).fetchone()
        if not migrated:
            conn.execute(
                "UPDATE telegraph_articles SET summary_source='telegraph' "
                "WHERE source_tag='آرشیو' AND summary_source='ai'"
            )
            conn.execute(
                "INSERT INTO telegraph_catalog_meta (key, value) "
                "VALUES ('summary_source_migrated', '1')"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegraph_articles_published "
            "ON telegraph_articles(published_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegraph_articles_source "
            "ON telegraph_articles(source_tag)"
        )


def _catalogue_url() -> str:
    """The public archive URL that every published page links to in its footer.

    Empty when ``articles`` cannot be imported, so a caller can skip the check
    rather than duplicate the constant here.
    """
    try:
        import articles

        return articles.ARTICLE_CATALOGUE_URL
    except Exception:
        return ""


def _host(url: str) -> str:
    return (urlparse(str(url or "").strip()).hostname or "").lower().removeprefix("www.")


def _youtube_video_id(host: str, path: str, query: dict[str, str]) -> str:
    if host == "youtu.be":
        return path.strip("/").split("/")[0]
    match = _YOUTUBE_PATH_ID_RE.match(path)
    return match.group(1) if match else query.get("v", "")


def source_key(url: str) -> str:
    """Return a comparable identity for a source-article URL.

    Sites link to their own articles with a mix of ``www``/bare hosts, trailing
    slashes, tracking parameters, AMP suffixes and — for the Premier League —
    an optional ``/en`` prefix plus a slug that can be edited after
    publication. Comparing raw URLs would therefore miss most links pointing at
    an article we have already translated. A URL that identifies no specific
    article (a homepage, a section index) returns an empty key so it can never
    match anything.
    """
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    path = re.sub(r"/{2,}", "/", unquote(parsed.path or "")).rstrip("/")
    query = {
        key: values[0]
        for key, values in parse_qs(parsed.query).items()
        if key.lower() not in _TRACKING_QUERY_PARAMS
    }

    if host in _YOUTUBE_HOSTS:
        video_id = _youtube_video_id(host, path, query)
        return f"youtube.com/watch?v={video_id}" if video_id else ""
    if host == "premierleague.com":
        match = _PL_NEWS_PATH_RE.match(path)
        if match:
            return f"{host}/news/{match.group(1)}"

    path = re.sub(r"/amp$", "", path, flags=re.IGNORECASE).lower()
    suffix = "".join(f"?{key}={value}" for key, value in sorted(query.items()))
    return f"{host}{path}{suffix}" if path or suffix else ""


def resolve_source_links(urls: Iterable[str]) -> dict[str, str]:
    """Map source-article URLs to our own published translation of each.

    Only articles that are public in the catalog are offered: a hidden row is
    a draft, an import that was never posted, or an article still waiting in
    the review queue, and none of those should be linked to from a new post.
    """
    wanted: dict[str, list[str]] = {}
    for url in urls:
        key = source_key(url)
        if key:
            wanted.setdefault(key, []).append(str(url))
    if not wanted:
        return {}

    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT url, source_url FROM telegraph_articles "
            "WHERE hidden=0 AND source_url <> '' ORDER BY published_at"
        ).fetchall()
    # Ordered oldest first, so an article translated more than once resolves to
    # its most recent Telegraph page.
    published: dict[str, str] = {}
    for row in rows:
        key = source_key(row["source_url"])
        if key in wanted:
            published[key] = str(row["url"])
    return {
        url: published[key]
        for key, matching_urls in wanted.items()
        if key in published
        for url in matching_urls
    }


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
    summary_source: str = "ai",
    source_url: str = "",
    hidden: bool = True,
) -> None:
    """Insert or update a locally indexed Telegraph page.

    Newly created Telegraph pages stay private to the catalog until the
    matching article link has actually appeared in the main Telegram channel.
    Existing records retain their visibility when their metadata is refreshed.
    """
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
                (path, url, title, summary, summary_source, source_tag,
                 source_url, image_url, hidden, published_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                url=excluded.url,
                title=excluded.title,
                summary=CASE
                    WHEN telegraph_articles.summary_source='ai'
                         AND excluded.summary_source='telegraph'
                    THEN telegraph_articles.summary
                    ELSE excluded.summary
                END,
                summary_source=CASE
                    WHEN telegraph_articles.summary_source='ai'
                    THEN 'ai'
                    ELSE excluded.summary_source
                END,
                source_tag=CASE
                    WHEN telegraph_articles.source_tag <> ''
                    THEN telegraph_articles.source_tag
                    ELSE excluded.source_tag
                END,
                source_url=CASE
                    WHEN excluded.source_url <> '' THEN excluded.source_url
                    ELSE telegraph_articles.source_url
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
                summary_source,
                _plain_text(source_tag),
                str(source_url or "").strip(),
                str(image_url or "").strip(),
                1 if hidden else 0,
                published_at,
                now,
            ),
        )


def list_source_tags() -> list[str]:
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT source_tag FROM telegraph_articles "
            "WHERE source_tag <> '' AND hidden=0 "
            "ORDER BY source_tag COLLATE NOCASE"
        ).fetchall()
    return [str(row[0]) for row in rows]


def list_pages(
    *, query: str = "", source_tag: str = "", limit: int = 24, offset: int = 0,
    include_hidden: bool = False,
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
    if not include_hidden:
        clauses.insert(0, "hidden=0")
    where = f"WHERE {' AND '.join(clauses)}"
    with _connect() as conn:
        rows = conn.execute(
            "SELECT path, url, title, summary, summary_source, source_tag, "
            "source_url, image_url, "
            "published_at, updated_at "
            f"FROM telegraph_articles {where} "
            "ORDER BY published_at DESC, path DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def pages_needing_enrichment(limit: int = 12) -> list[dict]:
    """Return indexed pages with metadata still worth recovering.

    ``source_url`` is included because internal-link rerouting can only find a
    page that knows which article it translated. Some pages can never be
    completed — an old import with neither an image nor a source link — so
    never-attempted pages come first and the rest are retried oldest-attempt
    first. That turns the unfixable remainder into a rotation instead of a
    permanent block on the rest of the backlog.
    """
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT path, url, title, summary, summary_source, source_tag, "
            "source_url, image_url "
            "FROM telegraph_articles "
            "WHERE hidden=0 AND "
            "(summary_source <> 'ai' OR summary='' OR image_url='' OR source_url='') "
            "ORDER BY enriched_at, published_at DESC LIMIT ?",
            (max(1, min(int(limit), 50)),),
        ).fetchall()
    return [dict(row) for row in rows]


def update_metadata(
    url: str,
    *,
    summary: str | None = None,
    image_url: str | None = None,
    summary_source: str | None = None,
    source_url: str | None = None,
) -> None:
    path = _path_from_url(url)
    if not path:
        return
    fields = []
    values: list[str] = []
    if summary is not None:
        fields.append("summary=?")
        values.append(_plain_text(summary))
    if image_url is not None:
        fields.append("image_url=?")
        values.append(str(image_url).strip())
    if source_url is not None:
        fields.append("source_url=?")
        values.append(str(source_url).strip())
    if summary_source is not None:
        fields.append("summary_source=?")
        values.append(summary_source)
    if not fields:
        return
    fields.append("updated_at=?")
    values.append(datetime.now(timezone.utc).isoformat())
    init()
    with _connect() as conn:
        conn.execute(
            f"UPDATE telegraph_articles SET {', '.join(fields)} WHERE path=?",
            (*values, path),
        )


def mark_enrichment_attempt(url: str) -> None:
    """Move a page to the back of the enrichment queue, fixed or not."""
    path = _path_from_url(url)
    if not path:
        return
    init()
    with _connect() as conn:
        conn.execute(
            "UPDATE telegraph_articles SET enriched_at=? WHERE path=?",
            (datetime.now(timezone.utc).isoformat(), path),
        )


def update_title(url: str, title: str) -> None:
    """Keep the local catalog title in sync after a Telegraph edit."""
    path = _path_from_url(url)
    if not path:
        return
    init()
    with _connect() as conn:
        conn.execute(
            "UPDATE telegraph_articles SET title=?, updated_at=? WHERE path=?",
            (
                _plain_text(title),
                datetime.now(timezone.utc).isoformat(),
                path,
            ),
        )


def set_hidden(url: str, hidden: bool = True) -> bool:
    """Hide or restore one indexed page in the public catalog."""
    path = _path_from_url(url)
    if not path:
        return False
    init()
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE telegraph_articles SET hidden=?, updated_at=? WHERE path=?",
            (
                1 if hidden else 0,
                datetime.now(timezone.utc).isoformat(),
                path,
            ),
        )
    return cursor.rowcount > 0


def publish_in_catalog(url: str, *, since: datetime | None = None) -> bool:
    """Make a recent, already-indexed article public after its channel post exists."""
    path = _path_from_url(url)
    if not path:
        return False
    init()
    clauses = ["path=?", "hidden=1"]
    values: list[str | int] = [path]
    if since is not None:
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        clauses.append("published_at>=?")
        values.append(since.astimezone(timezone.utc).isoformat())
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE telegraph_articles SET hidden=0, updated_at=? WHERE "
            + " AND ".join(clauses),
            (datetime.now(timezone.utc).isoformat(), *values),
        )
    return cursor.rowcount > 0


def hidden_paths() -> set[str]:
    """Return paths hidden from the public catalog."""
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT path FROM telegraph_articles WHERE hidden=1"
        ).fetchall()
    return {str(row[0]) for row in rows}


def first_image_url(html_content: str) -> str:
    match = re.search(
        r'<img\b[^>]*\bsrc=["\'](https?://[^"\']+)',
        str(html_content or ""),
        flags=re.IGNORECASE,
    )
    url = match.group(1) if match else ""
    return url if urlparse(url).scheme in {"http", "https"} else ""


def first_source_url(html_content: str) -> str:
    """Recover the original article URL from a published Telegraph page.

    Every page ends with a link to the public catalogue, and pages published
    since internal-link rerouting can also contain links to our other Telegraph
    articles. Neither is the source, so both are skipped and the labelled
    "منبع اصلی مقاله" link is preferred over any other outbound link.
    """
    catalogue_host = _host(_catalogue_url())
    candidates = []
    for url, label in _ANCHOR_RE.findall(str(html_content or "")):
        url = html.unescape(url)
        host = _host(url)
        if host in _TELEGRAPH_HOSTS or (catalogue_host and host == catalogue_host):
            continue
        candidates.append((url, _plain_text(label)))
    for url, label in reversed(candidates):
        if _SOURCE_LINK_LABEL in label:
            return url
    return candidates[-1][0] if candidates else ""


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
                summary_source="telegraph",
                # A Telegraph account can contain drafts or pages that were
                # never posted to the channel, so importing must not expose
                # previously unknown pages.
                hidden=True,
            )
            imported += 1
        if len(pages) < limit:
            break
        offset += limit
    return imported
