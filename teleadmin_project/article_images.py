"""Tell an article's own images apart from the banners every article carries.

Sources repeat the same promo-code banner, offer graphic, or house advert at
the top or bottom of every post. Matching them by filename or by wording is the
approach that already failed: each site changes both, and a rule tight enough
to catch one is tight enough to cut real content when the site is redesigned.

What separates them is not what they look like but how often they appear. An
image belonging to an article appears in that article and almost nowhere else;
a banner appears in article after article. So every image URL a source hands us
is recorded against the article it came from, and one seen in enough separate
articles stops being treated as content.

The count is deliberately over distinct articles rather than sightings: a retry
or a re-import of the same article must not be able to convict its own images.
"""
import logging
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import urlparse

import runtime_config

logger = logging.getLogger(__name__)

# Seen in this many separate articles and it is furniture, not content. The
# floor is 3 because a genuine photo does occasionally get reused once — a
# player portrait in two pieces about that player — while a banner reappears
# indefinitely.
_MIN_ARTICLES = 3
# Once a URL has this many sightings the verdict cannot change, so recording
# more of them only grows the table. This caps exactly the worst offenders.
_MAX_SIGHTINGS = 25


def _connect() -> sqlite3.Connection:
    runtime_config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(runtime_config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS article_image_sightings (
                image_key TEXT NOT NULL,
                article_key TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                seen_at TEXT NOT NULL,
                PRIMARY KEY (image_key, article_key)
            )
            """
        )


def image_key(url: str) -> str:
    """Return the identity of one image, ignoring per-article URL decoration.

    The same banner is served with different resize, cache-busting, and CDN
    query strings from one article to the next, so the query is dropped and the
    host and path alone decide whether two URLs are the same picture.
    """
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    return f"{host}{path}" if host and path else ""


def recurring_images(article_key: str, image_urls: Iterable[str]) -> set[str]:
    """Record this article's images and return those that recur across articles.

    ``article_key`` identifies the article, not the request: pass a canonical
    form so the same article seen at two URLs counts once.
    """
    candidates: dict[str, list[str]] = {}
    for url in image_urls:
        key = image_key(url)
        if key:
            candidates.setdefault(key, []).append(str(url))
    article_key = str(article_key or "").strip()
    if not candidates or not article_key:
        return set()

    init()
    now = datetime.now(timezone.utc).isoformat()
    recurring: set[str] = set()
    with _connect() as conn:
        placeholders = ",".join("?" * len(candidates))
        counts = {
            str(row["image_key"]): int(row["sightings"])
            for row in conn.execute(
                "SELECT image_key, count(*) AS sightings "
                f"FROM article_image_sightings WHERE image_key IN ({placeholders}) "
                "GROUP BY image_key",
                tuple(candidates),
            )
        }
        for key, urls in candidates.items():
            if counts.get(key, 0) < _MAX_SIGHTINGS:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO article_image_sightings "
                    "(image_key, article_key, image_url, seen_at) VALUES (?, ?, ?, ?)",
                    (key, article_key, urls[0], now),
                )
                counts[key] = counts.get(key, 0) + cursor.rowcount
            if counts.get(key, 0) >= _MIN_ARTICLES:
                recurring.update(urls)
    return recurring


def sightings(url: str) -> int:
    """How many separate articles this image has been seen in."""
    key = image_key(url)
    if not key:
        return 0
    init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT count(*) FROM article_image_sightings WHERE image_key=?", (key,)
        ).fetchone()
    return int(row[0]) if row else 0
