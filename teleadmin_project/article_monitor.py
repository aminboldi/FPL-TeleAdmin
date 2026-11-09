"""Poll the supported FPL article sources for new posts."""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

import runtime_config
import fplcore
from translator import ContentIncompleteError

logger = logging.getLogger(__name__)

_POLL_SECONDS = 15 * 60
_MAX_CANDIDATES_PER_SOURCE = 12
# Scout's RSS <category> elements include both section names and tags. Match
# known section labels exactly, rather than guessing from article titles or
# broad words such as "captain" (also used by the weekly recommendations).
_SCOUT_EXCLUDED_SECTIONS = frozenset(name.casefold() for name in (
    "Home Page Exclusions", "Dugout Discussion", "Scoreboard",
    "Team News", "Team Reveals", "Pro Pundits", "FPL Milanista",
))
# Other fantasy games have their own Scout sections. FPL itself remains
# eligible, including all its weekly recommendation sections.
_SCOUT_OTHER_GAMES = frozenset(name.casefold() for name in (
    "AFCON", "Allsvenskan Fantasy", "Asian Cup", "Champions League", "CLUB",
    "DraftKings", "DugoutFC",
    "Eliteserien Fantasy", "FanDuel", "Fanslide", "Fantasy Bet",
    "Fantasy Bundesliga", "Fantasy Club World Cup", "Fantasy EFL",
    "Fantasy Football Exchange", "Fantasy Football Scotland", "Fantasy League",
    "Fantasy Liga Portugal", "Fantasy5", "FanTeam", "FanTeam World Cup",
    "Fantrax", "FiT", "Football Fun", "Football Index", "FPL Challenge",
    "FPL Draft", "GAFFR Fantasy", "GoalScorer Challenge", "Kickest",
    "Mini League Mate", "MLS", "MLS Fantasy", "Mondogoal",
    "Nations League Fantasy", "PickGuru", "PlayOn", "Premier Punt",
    "Saudi Fantasy", "Serie A Fantasy", "Sky Sports", "Sky Sports - World Cup", "SoRare",
    "Spitch", "Tatttico Fantasy Serie A", "Türkiye Fantasy", "The Sun",
    "UFPL", "Ultimate Fan", "Women's Euro 2025 Fantasy", "World Cup",
    "World Cup 2014", "World Cup Fantasy 2026", "World Cup Games",
    "WSL Fantasy", "WUCL", "Yahoo", "zzzzzzzzDraftKings.",
    "Euro 2012", "Euro 2016", "Euro 2020", "Euro 2024",
))
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


def _scout_excluded_section(labels: list[str]) -> str:
    """Return the blocking label; exclusions win over any weekly series."""
    for label in labels:
        normalized = _plain_text(label).casefold()
        if (
            normalized in _SCOUT_EXCLUDED_SECTIONS
            or normalized in _SCOUT_OTHER_GAMES
            or normalized.startswith("pro pundits -")
        ):
            return label
    return ""


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
        if source_key == "fantasyfootballscout":
            labels = [
                category.get("term") or category.text or ""
                for category in element.findall("{*}category")
            ]
            excluded_section = _scout_excluded_section(labels)
            if excluded_section:
                logger.debug("Skipping Scout article %s: %s", url, excluded_section)
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




def _is_fff_article(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.hostname in {"fantasyfootballfix.com", "www.fantasyfootballfix.com"}
        and parsed.path.startswith("/blog-index/")
        and parsed.path.rstrip("/") != "/blog-index"
    )


def _fplarmo_candidates() -> list[Candidate]:
    return [
        Candidate(
            fplcore.SOURCE_KEY,
            _plain_text(post.get("title", {}).get("rendered", "")),
            fplcore.post_url(post),
            (post.get("date_gmt") or post.get("date", "")) + "Z",
        )
        for post in fplcore.discover_posts(_MAX_CANDIDATES_PER_SOURCE)
    ]


def discover_candidates() -> list[Candidate]:
    """Fetch the latest candidates from the configured automatic sources."""
    # Premier League URLs remain supported for explicit/manual imports, but
    # the site's automatic feed is intentionally disabled due to poor FPL
    # article classification.
    sources = (
        ("fantasyfootballfix", lambda: _listing_candidates(
            "fantasyfootballfix", "https://www.fantasyfootballfix.com/blog-index/", _is_fff_article
        )),
        ("fantasyfootballscout", lambda: _rss_candidates(
            "fantasyfootballscout", "https://www.fantasyfootballscout.co.uk/feed/"
        )),
        ("allaboutfpl", lambda: _rss_candidates(
            "allaboutfpl", "https://allaboutfpl.com/feed/"
        )),
        (fplcore.SOURCE_KEY, _fplarmo_candidates),
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


# A feed that serves a truncated article usually fills in within a few hours,
# so retry on the normal backoff before giving up on it for good.
_MAX_INCOMPLETE_ATTEMPTS = 4


async def _poll_once(import_article, on_abandoned=None, *, import_fplarmo=None) -> None:
    candidates = await asyncio.to_thread(discover_candidates)
    # Adding a source to an already-running monitor must not publish its
    # historical backlog. Keep this source's baseline separate from the
    # existing global marker, which older deployments already have.
    armo_candidates = [c for c in candidates if c.source_key == fplcore.SOURCE_KEY]
    if armo_candidates and not runtime_config.article_monitor_is_bootstrapped(fplcore.SOURCE_KEY):
        runtime_config.seed_article_monitor_candidates([c.as_dict() for c in armo_candidates])
        runtime_config.mark_article_monitor_bootstrapped(fplcore.SOURCE_KEY)
        candidates = [c for c in candidates if c.source_key != fplcore.SOURCE_KEY]
        logger.info("Initialized FPLArmo monitor with %d existing articles", len(armo_candidates))
    pending = [Candidate(**row) for row in runtime_config.pending_article_relays()]
    candidates = list({c.url: c for c in [*candidates, *pending]}.values())
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
        is_armo = candidate.source_key == fplcore.SOURCE_KEY
        retry_options = {"retry_after_seconds": _POLL_SECONDS} if is_armo else {}
        if not runtime_config.claim_article_monitor_candidate(candidate_dict, **retry_options):
            continue
        try:
            if is_armo and import_fplarmo is None:
                raise RuntimeError("FPLArmo immediate publication route is not configured")
            importer = import_fplarmo if is_armo else import_article
            imported = await importer(candidate.url)
            if not imported:
                raise RuntimeError("article extraction returned no readable content")
        except Exception as exc:
            # An incomplete source is expected to be retryable, so it is capped
            # and reported rather than retried forever. Other failures keep the
            # previous unlimited-retry behaviour.
            incomplete = isinstance(exc, ContentIncompleteError)
            abandoned = runtime_config.finish_article_monitor_candidate(
                candidate.url,
                success=False,
                error=str(exc),
                max_attempts=_MAX_INCOMPLETE_ATTEMPTS if incomplete else 0,
            )
            if incomplete:
                logger.warning(
                    "Article %s looks incomplete (%s); %s",
                    candidate.url,
                    exc,
                    "giving up" if abandoned else "will retry",
                )
            else:
                logger.exception("Automatic article import failed for %s", candidate.url)
            if abandoned and on_abandoned:
                try:
                    await on_abandoned(candidate.url, exc)
                except Exception:
                    logger.exception("Could not report abandoned article %s", candidate.url)
        else:
            runtime_config.finish_article_monitor_candidate(candidate.url, success=True)
            logger.info("Automatically imported article %s", candidate.url)


async def run_monitor(import_article, on_abandoned=None, *, import_fplarmo=None) -> None:
    """Poll sources indefinitely and hand new URLs to the normal importer."""
    logger.info("Article monitor started; polling every %d minutes", _POLL_SECONDS // 60)
    while True:
        try:
            if runtime_config.get_bool("ARTICLE_MONITOR_ENABLED"):
                await _poll_once(import_article, on_abandoned, import_fplarmo=import_fplarmo)
        except Exception:
            logger.exception("Article monitor cycle failed")
        await asyncio.sleep(_POLL_SECONDS)
