"""Fetch and extract Premier League article content for translation."""
import logging
import os
import re
from html import escape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from telegraph import Telegraph
import trafilatura

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

_GENERAL_ARTICLE_MIN_CHARS = 500
_PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscribe to read",
    "sign in to continue",
    "sign in to read",
    "this content is for subscribers",
    "this article is for subscribers",
    "subscriber-only content",
    "premium content",
)
_TELEGRAPH_TAGS = {
    "a", "b", "blockquote", "br", "code", "em", "figure", "figcaption",
    "h3", "h4", "hr", "i", "img", "li", "ol", "p", "pre", "s",
    "strong", "u", "ul",
}

_telegraph: Telegraph | None = None


def _get_telegraph() -> Telegraph:
    global _telegraph
    if _telegraph is not None:
        return _telegraph

    token = os.getenv("TELEGRAPH_ACCESS_TOKEN")
    if token:
        _telegraph = Telegraph(access_token=token)
        logger.info("Using existing Telegraph account")
        return _telegraph

    _telegraph = Telegraph()
    try:
        result = _telegraph.create_account(short_name="TeleAdmin")
        token = result.get("access_token", "")
        if token:
            logger.info(
                "New Telegraph account created. "
                "To manage articles manually, set TELEGRAPH_ACCESS_TOKEN=%s in .env",
                token,
            )
        else:
            logger.warning("Telegraph account created but no token returned")
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


def _fetch_pl_article(url: str) -> dict | None:
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


def _metadata_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def _telegraph_safe_article_html(extracted_html: str, base_url: str) -> tuple[str, list[str]]:
    """Keep reader-mode structure while restricting it to Telegraph-safe HTML."""
    soup = BeautifulSoup(extracted_html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()

    images: list[str] = []
    for tag in soup.find_all(True):
        if tag.name in {"h1", "h2"}:
            tag.name = "h3"
        elif tag.name not in _TELEGRAPH_TAGS:
            tag.unwrap()
            continue

        if tag.name == "img":
            src = str(tag.get("src") or "").strip()
            src = urljoin(base_url, src)
            if not src.startswith(("http://", "https://")):
                tag.decompose()
                continue
            tag.attrs = {"src": src}
            images.append(src)
        elif tag.name == "a":
            href = str(tag.get("href") or "").strip()
            href = urljoin(base_url, href)
            tag.attrs = {"href": href} if href.startswith(("http://", "https://")) else {}
        else:
            tag.attrs = {}

    root = soup.body or soup
    html = "".join(str(child) for child in root.contents).strip()
    return html, list(dict.fromkeys(images))


def _source_article_images(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Collect article-local image URLs when reader-mode output omits them."""
    container = soup.find("article") or soup.find("main") or soup
    images = []
    for image in container.find_all("img"):
        src = image.get("src") or image.get("data-src") or image.get("data-original")
        if not src:
            continue
        src = urljoin(base_url, str(src).strip())
        if src.startswith(("http://", "https://")):
            images.append(src)
    if not images:
        og_image = _metadata_content(soup, "og:image", "twitter:image")
        if og_image:
            images.append(urljoin(base_url, og_image))
    return list(dict.fromkeys(images))


def fetch_general_article(url: str) -> dict | None:
    """Extract a readable, server-rendered article from an arbitrary web page."""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        response = requests.get(url, headers=_HEADERS, timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
        logger.info("Could not fetch general article %s: %s", url, exc)
        return None

    if response.status_code in {401, 402, 403, 451}:
        logger.info("Skipping blocked or paywalled article %s", url)
        return None
    if not response.ok or "html" not in response.headers.get("content-type", "").lower():
        return None

    extracted = trafilatura.extract(
        response.text,
        url=response.url,
        output_format="html",
        include_comments=False,
        include_formatting=True,
        include_links=True,
        include_images=True,
        favor_precision=True,
        deduplicate=True,
    )
    if not extracted:
        return None

    content_html, images = _telegraph_safe_article_html(extracted, response.url)
    readable_text = BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True)
    if len(readable_text) < _GENERAL_ARTICLE_MIN_CHARS:
        return None

    page_lower = response.text.lower()
    if len(readable_text) < 2000 and any(marker in page_lower for marker in _PAYWALL_MARKERS):
        logger.info("Skipping likely paywalled article %s", response.url)
        return None

    page = BeautifulSoup(response.text, "html.parser")
    if not images:
        images = _source_article_images(page, response.url)
    title = _metadata_content(page, "og:title", "twitter:title") or (
        page.title.get_text(" ", strip=True) if page.title else ""
    )
    if not title:
        return None
    return {
        "title": title,
        "summary": _metadata_content(page, "og:description", "description"),
        "url": response.url,
        "html": content_html,
        "images": images,
    }


def fetch_article(url: str) -> dict | None:
    """Use the site-specific extractor when available, reader mode otherwise."""
    if is_pl_article_url(url):
        return _fetch_pl_article(url)
    return fetch_general_article(url)


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


def build_general_article_html(article: dict) -> str:
    """Wrap reader-mode content with a title and canonical source link."""
    return (
        f"<h3>{escape(article['title'])}</h3>"
        f"{article['html']}"
        f'<p><a href="{escape(article["url"], quote=True)}">پست اصلی</a></p>'
    )


def restore_missing_images(html_content: str, image_urls: list[str]) -> str:
    """Append any source images omitted by the model to the Telegraph article."""
    present = set(re.findall(r'<img[^>]+src=["\']([^"\']+)', html_content, re.IGNORECASE))
    missing = [url for url in image_urls if url and url not in present]
    if not missing:
        return html_content
    return html_content + "".join(f'<img src="{escape(url, quote=True)}">' for url in missing)


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
