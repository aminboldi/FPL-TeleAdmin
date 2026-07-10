"""Fetch and extract Premier League article content for translation."""
import logging
import re

import requests
from bs4 import BeautifulSoup
from telegraph import Telegraph

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

_PL_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?premierleague\.com/(?:en/)?news/\d+[^\s]*"
)

_SHORT_URL_RE = re.compile(
    r"(?:https?://)?preml\.ge/\S+"
)

_TCO_URL_RE = re.compile(
    r"https?://t\.co/\S+"
)

_telegraph: Telegraph | None = None


def _get_telegraph() -> Telegraph:
    global _telegraph
    if _telegraph is None:
        _telegraph = Telegraph()
        try:
            _telegraph.create_account(short_name="TeleAdmin")
        except Exception:
            pass
    return _telegraph


def is_pl_article_url(text: str, entities: list | None = None) -> bool:
    if text and (_PL_URL_RE.search(text) or _SHORT_URL_RE.search(text) or _TCO_URL_RE.search(text)):
        return True
    for m in re.finditer(r"https?://\S+", text or ""):
        raw = m.group(0)
        if _PL_URL_RE.search(raw) or _SHORT_URL_RE.search(raw) or _TCO_URL_RE.search(raw):
            return True
    if entities:
        for e in entities:
            url = getattr(e, "url", None)
            if url and (
                _PL_URL_RE.search(url) or _SHORT_URL_RE.search(url) or _TCO_URL_RE.search(url)
            ):
                return True
    return False

def resolve_url(text: str, entities: list | None = None) -> str | None:
    for m in _SHORT_URL_RE.finditer(text or ""):
        return _ensure_https(m.group(0))
    for m in _TCO_URL_RE.finditer(text or ""):
        return m.group(0)
    for m in _PL_URL_RE.finditer(text or ""):
        return _ensure_https(m.group(0))
    if entities:
        for e in entities:
            url = getattr(e, "url", None)
            if url and (
                _PL_URL_RE.search(url) or _SHORT_URL_RE.search(url) or _TCO_URL_RE.search(url)
            ):
                return url
    return None


def _ensure_https(url: str) -> str:
    if not url.startswith("http"):
        return "https://" + url
    return url


def fetch_article(url: str) -> dict | None:
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Failed to fetch article %s: %s", url, e)
        return None

    final_url = resp.url
    soup = BeautifulSoup(resp.text, "html.parser")

    title_el = soup.select_one(".article__header-title")
    title = title_el.get_text(strip=True) if title_el else ""

    summary_el = soup.select_one(".article__summary")
    summary = summary_el.get_text(strip=True) if summary_el else ""

    date_el = soup.select_one(".article__publish-date")
    date_str = date_el.get_text(strip=True) if date_el else ""

    header_image = ""

    header_img = soup.select_one(".article__header-image img")
    if header_img:
        src = header_img.get("src") or header_img.get("data-src") or ""
        if src:
            header_image = src

    content_el = soup.select_one(".article__content")
    if not content_el:
        return None

    for widget in content_el.select(
        ".articleWidget, .embeddable-article, .article-related-content, "
        ".media-actions, .article__share-container"
    ):
        widget.decompose()

    parts = []
    for child in content_el.children:
        if not hasattr(child, "name"):
            continue
        tag = child.name
        if tag == "p":
            text = child.get_text(strip=True)
            if text and not text.startswith("Share"):
                parts.append({"type": "p", "text": text})
        elif tag in ("figure", "picture"):
            img = child.find("img")
            if img:
                src = img.get("src") or img.get("data-src") or ""
                if src:
                    parts.append({"type": "img", "src": src})

    if not parts:
        raw_text = content_el.get_text(separator="\n", strip=True)
        if raw_text:
            parts = [{"type": "p", "text": raw_text}]

    return {
        "title": title,
        "summary": summary,
        "date": date_str,
        "parts": parts,
        "url": final_url,
        "header_image": header_image,
    }


def build_article_html(title: str, date: str, summary: str, parts: list[dict], original_url: str, header_image: str = "") -> str:
    result = [f"<h3>{title}</h3>"]
    if date:
        result.append(f"<p><b>{date}</b></p>")
    if header_image:
        result.append(f'<img src="{header_image}">')
    if summary:
        result.append(f"<p><b>{summary}</b></p>")
    for part in parts:
        if part["type"] == "p":
            result.append(f"<p>{part['text']}</p>")
        elif part["type"] == "img":
            src = part["src"]
            result.append(f'<img src="{src}">')
    result.append(f'<p><a href="{original_url}">پست اصلی</a></p>')
    return "".join(result)


def publish_to_telegraph(title: str, html_content: str) -> str | None:
    try:
        tg = _get_telegraph()
        page = tg.create_page(title=title, html_content=html_content)
        url = page.get("url", "")
        logger.info("Telegraph page created: %s", url)
        return url
    except Exception as e:
        logger.error("Telegraph publish failed: %s", e)
        return None
