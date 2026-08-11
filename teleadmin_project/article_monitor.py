"""Poll the supported FPL article sources for new posts."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

import runtime_config
import articles

logger = logging.getLogger(__name__)

_POLL_SECONDS = 15 * 60
_MAX_CANDIDATES_PER_SOURCE = 12
# The unfiltered page contains every Premier League publication.  The
# `type=Fantasy` channel is the official PL classification for FPL articles.
_PL_NEWS_URL = "https://www.premierleague.com/en/news?type=Fantasy"
_PL_CONTENT_API_BASE = "https://api.premierleague.com/content/premierleague/playlist/EN"
_PL_SITEMAP_INDEX_URL = "https://www.premierleague.com/en/sitemap/index.xml"
_PL_FANTASY_SLUG_RE = re.compile(
    r"(?:fantasy|\bfpl\b|the-scout|scout-selection|gameweek|captain|"
    r"wildcard|bench-boost|triple-captain|free-hit|price-reveal)",
    re.IGNORECASE,
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/120.0.0.0 Safari/537.36 TeleAdminArticleMonitor/1.0"
    )
}


@dataclass(frozen=True)
class Candidate:
    source_key: str
    title: str
    url: str
    published_at: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "source_key": self.source_key,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
        }


def _normalize_url(url: str, base_url: str = "") -> str:
    url = urljoin(base_url, unescape(str(url or "").strip()))
    url, _ = urldefrag(url)
    return url.rstrip(".,;:!?)]}")


def _fetch(url: str) -> str:
    response = requests.get(url, headers=_HEADERS, timeout=25)
    response.raise_for_status()
    return response.text


def _rss_candidates(source_key: str, feed_url: str) -> list[Candidate]:
    root = ET.fromstring(_fetch(feed_url))
    candidates = []
    elements = list(root.findall(".//item")) + list(root.findall(".//{*}entry"))
    for element in elements:
        link = element.findtext("link", default="")
        if not link:
            for link_element in element.findall("{*}link"):
                link = link_element.get("href", "")
                if link:
                    break
        url = _normalize_url(link, feed_url)
        if not url.startswith(("http://", "https://")):
            continue
        title = element.findtext("title", default="")
        published_at = (
            element.findtext("pubDate", default="")
            or element.findtext("{*}published", default="")
            or element.findtext("{*}updated", default="")
        )
        candidates.append(Candidate(source_key, _plain_text(title), url, published_at.strip()))
    return candidates[:_MAX_CANDIDATES_PER_SOURCE]


def _plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(unescape(value or ""), "html.parser").get_text(" ")).strip()


def _listing_candidates(
    source_key: str,
    listing_url: str,
    link_matcher,
) -> list[Candidate]:
    return _listing_candidates_from_html(
        source_key, listing_url, _fetch(listing_url), link_matcher
    )


def _listing_candidates_from_html(
    source_key: str,
    listing_url: str,
    html: str,
    link_matcher,
) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        url = _normalize_url(anchor.get("href", ""), listing_url)
        if url in seen or not link_matcher(url):
            continue
        seen.add(url)
        title = _plain_text(anchor.get_text(" "))
        if not title:
            title = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        candidates.append(Candidate(source_key, title, url))
        if len(candidates) >= _MAX_CANDIDATES_PER_SOURCE:
            break
    return candidates


def _premier_league_candidates() -> list[Candidate]:
    """Read fantasy-only PL news cards from the public content API.

    The PL news page now renders its article grid in JavaScript. The filtered
    HTML contains the fantasy playlist configuration, but not the current
    article links. Keep scraping that same filtered HTML as a fallback for
    older/site-degraded responses, then use the playlist API that the page
    itself calls for the live cards. If both expose no cards, use the official
    sitemap's clearly Fantasy-related URLs; never use the unfiltered listing.
    """
    page_html = _fetch(_PL_NEWS_URL)
    html_candidates = _listing_candidates_from_html(
        "premierleague", _PL_NEWS_URL, page_html, _is_premier_league_article
    )

    page = BeautifulSoup(page_html, "html.parser")
    grid = page.select_one(
        'div.content-grid[data-widget="content-list/content-grid-responsive"]'
    )
    playlist_id = str(grid.get("data-playlist-id", "")).strip() if grid else ""
    if not playlist_id.isdigit():
        logger.warning("Premier League news page did not expose a playlist ID; trying the official sitemap")
        return html_candidates or _premier_league_sitemap_candidates()

    api_url = (
        f"{_PL_CONTENT_API_BASE}/{playlist_id}"
        f"?pageSize={_MAX_CANDIDATES_PER_SOURCE}&detail=DETAILED"
    )
    try:
        payload = json.loads(_fetch(api_url))
    except Exception:
        logger.exception("Failed to read the Premier League news playlist API")
        return html_candidates or _premier_league_sitemap_candidates()

    candidates = []
    seen: set[str] = set()
    for item in payload.get("items", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        content = item.get("response")
        if not isinstance(content, dict):
            content = item
        content_type = str(content.get("type") or item.get("type") or "").lower()
        if content_type not in {"text", "article"}:
            continue

        content_id = content.get("id") or item.get("id")
        if not str(content_id or "").isdigit():
            continue
        slug = str(content.get("titleUrlSegment") or "").strip(" /")
        url = f"https://www.premierleague.com/en/news/{content_id}"
        if slug:
            url += f"/{slug}"
        if url in seen or not _is_premier_league_article(url):
            continue
        seen.add(url)
        published_at = content.get("date") or content.get("publishFrom") or ""
        candidates.append(
            Candidate(
                "premierleague",
                _plain_text(content.get("title", "")),
                url,
                str(published_at),
            )
        )
        if len(candidates) >= _MAX_CANDIDATES_PER_SOURCE:
            break
    return (candidates or html_candidates or _premier_league_sitemap_candidates())[:_MAX_CANDIDATES_PER_SOURCE]


def _xml_loc_entries(xml: str) -> list[tuple[str, str]]:
    """Return (URL, lastmod) pairs from either a sitemap index or urlset."""
    root = ET.fromstring(xml)
    entries = []
    for element in root:
        loc = element.findtext("{*}loc", default="").strip()
        if not loc:
            continue
        entries.append((loc, element.findtext("{*}lastmod", default="").strip()))
    return entries


def _premier_league_sitemap_candidates() -> list[Candidate]:
    """Fallback when the JS-only Fantasy listing exposes no usable feed.

    This deliberately reads only PL's own sitemap and only keeps news slugs
    with a strong Fantasy/Scout signal.  It is a safety net for an empty
    official playlist, not a replacement for the site's Fantasy taxonomy.
    """
    try:
        sitemap_entries = _xml_loc_entries(_fetch(_PL_SITEMAP_INDEX_URL))
    except Exception:
        logger.exception("Failed to read the Premier League sitemap index")
        return []

    # A sitemap index normally has a small number of dated news shards.  The
    # newest ones are at the end, but accept either ordering to tolerate a
    # future PL sitemap change.
    shard_urls = [
        url for url, _ in sitemap_entries
        if "sitemap" in url.casefold() and any(
            marker in url.casefold() for marker in ("news", "article", "content")
        )
    ]
    if not shard_urls:
        shard_urls = [url for url, _ in sitemap_entries if "sitemap" in url.casefold()]

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for shard_url in reversed(shard_urls[-6:]):
        try:
            entries = _xml_loc_entries(_fetch(shard_url))
        except Exception:
            logger.warning("Could not read Premier League sitemap shard %s", shard_url)
            continue
        for url, lastmod in reversed(entries):
            url = _normalize_url(url)
            if (
                url in seen
                or not _is_premier_league_article(url)
                or not _PL_FANTASY_SLUG_RE.search(urlparse(url).path)
            ):
                continue
            seen.add(url)
            slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
            candidates.append(Candidate("premierleague", slug.replace("-", " "), url, lastmod))

    candidates.sort(key=lambda candidate: candidate.published_at or candidate.url, reverse=True)
    return candidates[:_MAX_CANDIDATES_PER_SOURCE]


def _is_premier_league_article(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.hostname in {"premierleague.com", "www.premierleague.com"}
        and re.match(r"^/(?:en/)?news/\d+", parsed.path) is not None
        and not articles.is_excluded_premier_league_article(url)
    )


def _is_fff_article(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.hostname in {"fantasyfootballfix.com", "www.fantasyfootballfix.com"}
        and parsed.path.startswith("/blog-index/")
        and parsed.path.rstrip("/") != "/blog-index"
    )


def discover_candidates() -> list[Candidate]:
    """Fetch the latest candidates from all four supported sources."""
    sources = (
        ("premierleague", _premier_league_candidates),
        ("fantasyfootballfix", lambda: _listing_candidates(
            "fantasyfootballfix", "https://www.fantasyfootballfix.com/blog-index/", _is_fff_article
        )),
        ("fantasyfootballscout", lambda: _rss_candidates(
            "fantasyfootballscout", "https://www.fantasyfootballscout.co.uk/feed/"
        )),
        ("allaboutfpl", lambda: _rss_candidates(
            "allaboutfpl", "https://allaboutfpl.com/feed/"
        )),
    )
    candidates = []
    for source_key, fetcher in sources:
        try:
            source_candidates = fetcher()
        except Exception:
            logger.exception("Article monitor failed to read %s", source_key)
            continue
        candidates.extend(source_candidates)
        logger.info("Article monitor found %d candidates from %s", len(source_candidates), source_key)
    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.url, candidate)
    return list(unique.values())


async def _poll_once(import_article) -> None:
    candidates = await asyncio.to_thread(discover_candidates)
    if not candidates:
        return
    if not runtime_config.article_monitor_is_bootstrapped():
        runtime_config.seed_article_monitor_candidates(
            [candidate.as_dict() for candidate in candidates]
        )
        runtime_config.mark_article_monitor_bootstrapped()
        logger.info(
            "Article monitor initialized with %d existing candidates; future articles will be queued",
            len(candidates),
        )
        return

    candidates.sort(key=lambda candidate: candidate.published_at or candidate.url)
    for candidate in candidates:
        candidate_dict = candidate.as_dict()
        if not runtime_config.claim_article_monitor_candidate(candidate_dict):
            continue
        try:
            imported = await import_article(candidate.url)
            if not imported:
                raise RuntimeError("article extraction returned no readable content")
        except Exception as exc:
            runtime_config.finish_article_monitor_candidate(
                candidate.url, success=False, error=str(exc)
            )
            logger.exception("Automatic article import failed for %s", candidate.url)
        else:
            runtime_config.finish_article_monitor_candidate(candidate.url, success=True)
            logger.info("Automatically queued article %s", candidate.url)


async def run_monitor(import_article) -> None:
    """Poll sources indefinitely and hand new URLs to the normal importer."""
    logger.info("Article monitor started; polling every %d minutes", _POLL_SECONDS // 60)
    while True:
        try:
            if runtime_config.get_bool("ARTICLE_MONITOR_ENABLED"):
                await _poll_once(import_article)
        except Exception:
            logger.exception("Article monitor cycle failed")
        await asyncio.sleep(_POLL_SECONDS)
