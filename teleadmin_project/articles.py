"""Fetch and extract Premier League article content for translation."""
import logging
import os
import re
from html import escape
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment, NavigableString
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
_PL_WHO_AM_I_PATH_RE = re.compile(r"(?:^|/)who-am-i(?:[-/]|$)", re.IGNORECASE)

_SHORT_URL_RE = re.compile(
    r"(?:https?://)?preml\.ge/\S+"
)

_TCO_URL_RE = re.compile(
    r"https?://t\.co/\S+"
)

_PL_PROMOTIONAL_SELECTORS = (
    ".articleWidget",
    ".embeddable-article",
    ".article-related-content",
    ".media-actions",
    ".article__share-container",
    "a.content-card",
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
_FFFIX_HOSTS = {"fantasyfootballfix.com", "www.fantasyfootballfix.com"}
_FFSCOUT_HOSTS = {
    "fantasyfootballscout.co.uk",
    "www.fantasyfootballscout.co.uk",
}
_ALLABOUTFPL_HOSTS = {"allaboutfpl.com", "www.allaboutfpl.com"}
_ARTICLE_SOURCE_NAMES = {
    "fantasyfootballfix.com": "Fantasy Football Fix",
    "fantasyfootballscout.co.uk": "Fantasy Football Scout",
    "allaboutfpl.com": "AllAboutFPL",
    "premierleague.com": "Premier League",
}
_FFFIX_PROMO_LINKS = {
    "/premium/",
    "/reveal/",
    "/blog-index/",
}
_FFFIX_PROMO_LABELS = (
    "claim 50% off premium plus today",
    "track elite manager team changes",
    "stay ahead with expert fpl tips",
)
_TELEGRAPH_TAGS = {
    "a", "b", "blockquote", "br", "code", "em", "figure", "figcaption",
    "h3", "h4", "hr", "i", "img", "li", "ol", "p", "pre", "s",
    "strong", "u", "ul",
}
ARTICLE_CATALOGUE_URL = "https://epl-fantasy.ir"
_ARTICLE_CATALOGUE_FOOTER = (
    f'<p><a href="{ARTICLE_CATALOGUE_URL}">آرشیو مقالات کانال</a></p>'
)
_TELEGRAPH_PAGE_HOSTS = {
    "telegra.ph", "www.telegra.ph", "graph.org", "www.graph.org",
}
_SLUG_WORDS = {
    "آخرین": "akharin", "فانتزی": "fantasy", "درفت": "draft",
    "رتبه": "rank", "فصل": "season", "هفته": "week", "مقاله": "article",
    "ویدیو": "video", "بازیکن": "player", "بازی": "match", "تیم": "team",
}
_PERSIAN_TO_FINGLISH = str.maketrans({
    "ا": "a", "آ": "a", "ب": "b", "پ": "p", "ت": "t", "ث": "s",
    "ج": "j", "چ": "ch", "ح": "h", "خ": "kh", "د": "d", "ذ": "z",
    "ر": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s",
    "ض": "z", "ط": "t", "ظ": "z", "ع": "", "غ": "gh", "ف": "f",
    "ق": "gh", "ک": "k", "گ": "g", "ل": "l", "م": "m", "ن": "n",
    "و": "v", "ه": "h", "ی": "y", "ى": "y", "ئ": "y", "ؤ": "v",
    "ة": "h", "ء": "", "ە": "h", "٠": "0", "١": "1", "٢": "2",
    "٣": "3", "٤": "4", "٥": "5", "٦": "6", "٧": "7", "٨": "8",
    "٩": "9", "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
})

_telegraph: Telegraph | None = None


class TelegraphPublishError(RuntimeError):
    """Raised when Telegraph rejects an article publication."""


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


def get_telegraph_page_list(limit: int = 200, offset: int = 0) -> list[dict]:
    """Return the newest pages from the configured Telegraph account."""
    if not os.getenv("TELEGRAPH_ACCESS_TOKEN"):
        raise RuntimeError(
            "TELEGRAPH_ACCESS_TOKEN is not configured; Telegraph editing is unavailable."
        )
    result = _get_telegraph().get_page_list(limit=limit, offset=offset)
    pages = result.get("pages", [])
    return pages if isinstance(pages, list) else []


def get_recent_telegraph_pages(limit: int = 10) -> list[dict]:
    return get_telegraph_page_list(limit=limit)


def _telegraph_path(page_url_or_path: str) -> str:
    """Return the API page path from a Telegraph URL or bare path."""
    value = (page_url_or_path or "").strip()
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "telegra.ph", "www.telegra.ph", "graph.org", "www.graph.org",
        }:
            raise ValueError("The selected URL is not a Telegraph page.")
        value = parsed.path
    path = unquote(value).strip("/")
    if not path or "/" in path:
        raise ValueError("The Telegraph page path is invalid.")
    return path


def get_telegraph_page(page_url_or_path: str) -> dict:
    """Fetch an owned page, including its editable HTML content."""
    if not os.getenv("TELEGRAPH_ACCESS_TOKEN"):
        raise RuntimeError(
            "TELEGRAPH_ACCESS_TOKEN is not configured; Telegraph editing is unavailable."
        )
    return _get_telegraph().get_page(
        _telegraph_path(page_url_or_path), return_content=True, return_html=True
    )


def edit_telegraph_page(page_url_or_path: str, title: str, html_content: str) -> dict:
    """Update an existing page through Telegraph's supported editPage API."""
    if not os.getenv("TELEGRAPH_ACCESS_TOKEN"):
        raise RuntimeError(
            "TELEGRAPH_ACCESS_TOKEN is not configured; Telegraph editing is unavailable."
        )
    title = title.strip()
    if not title or len(title) > 256:
        raise ValueError("The article title must contain 1 to 256 characters.")
    return _get_telegraph().edit_page(
        path=_telegraph_path(page_url_or_path),
        title=title,
        html_content=normalize_telegraph_structure(html_content),
    )


def is_pl_article_url(text: str, entities: list | None = None) -> bool:
    # A t.co URL is not evidence that its destination is a PL article: it is
    # used by every X post.  Treating it as one sent arbitrary X links through
    # the brittle PL-only parser.  ``fetch_general_article`` follows redirects
    # safely for those links instead.
    if text and (_PL_URL_RE.search(text) or _SHORT_URL_RE.search(text)):
        return True
    for m in re.finditer(r"https?://\S+", text or ""):
        raw = m.group(0)
        if _PL_URL_RE.search(raw) or _SHORT_URL_RE.search(raw):
            return True
    if entities:
        for e in entities:
            url = getattr(e, "url", None)
            if url and (
                _PL_URL_RE.search(url) or _SHORT_URL_RE.search(url)
            ):
                return True
    return False


def is_excluded_premier_league_article(url: str) -> bool:
    """Return whether a Premier League URL is outside the FPL feed scope."""
    parsed = urlparse(url)
    return (
        parsed.hostname in {"premierleague.com", "www.premierleague.com"}
        and _PL_WHO_AM_I_PATH_RE.search(unquote(parsed.path)) is not None
    )

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


def article_source_name(url: str) -> str:
    """Return the publication name used in the Telegram article header."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return _ARTICLE_SOURCE_NAMES.get(host, host or "منبع وب")


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
            header_image = urljoin(final_url, src)

    content_el = soup.select_one(".article__content")
    if not content_el:
        # The PL frontend has changed its article classes several times.  The
        # generic extractor is intentionally our compatibility path: it uses
        # the semantic article/main content rather than a private CSS class,
        # retains the official metadata, and follows future server-rendered
        # layouts without requiring another scraper rewrite.
        logger.info("PL legacy article container missing; using reader-mode fallback for %s", final_url)
        return fetch_general_article(final_url)

    # The PL site uses content cards/widgets for promotions and related
    # articles. Remove them before walking the article body. Related Content
    # normally sits just outside ``article__content``, but removing it here as
    # well keeps the boundary safe if the frontend moves it inside later.
    for widget in soup.select(", ".join(_PL_PROMOTIONAL_SELECTORS)):
        widget.decompose()

    rerouted_links = reroute_internal_links(content_el, final_url)
    parts = []
    for child in content_el.children:
        if not hasattr(child, "name"):
            continue
        tag = child.name
        if tag == "p":
            text = child.get_text(strip=True)
            if text and not text.startswith("Share"):
                parts.append({
                    "type": "p",
                    "text": text,
                    "html": _paragraph_html_with_links(child, rerouted_links),
                })
        elif tag in ("figure", "picture"):
            img = child.find("img")
            if img:
                src = img.get("src") or img.get("data-src") or ""
                if src:
                    parts.append({"type": "img", "src": urljoin(final_url, src)})

    if not parts:
        raw_text = content_el.get_text(separator="\n", strip=True)
        if raw_text:
            parts = [{"type": "p", "text": raw_text}]

    if not title or not parts:
        logger.info("PL legacy article extraction was incomplete; using reader-mode fallback for %s", final_url)
        return fetch_general_article(final_url)

    return {
        "title": title,
        "summary": summary,
        "date": date_str,
        "parts": parts,
        "url": final_url,
        "header_image": header_image,
        "feature_image": header_image or next(
            (part["src"] for part in parts if part["type"] == "img"), ""
        ),
        "source_name": article_source_name(final_url),
    }


def _inline_paragraph_html(node, rerouted_links: set[str]) -> str:
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return escape(str(node), quote=False)
    if node.name in {"script", "style"}:
        return ""
    if node.name == "br":
        return "<br>"
    inner = "".join(
        _inline_paragraph_html(child, rerouted_links) for child in node.children
    )
    href = str(node.get("href") or "").strip() if node.name == "a" else ""
    if href and href in rerouted_links and node.get_text(strip=True):
        return f'<a href="{escape(href, quote=True)}">{inner}</a>'
    return inner


def _paragraph_html_with_links(paragraph, rerouted_links: set[str]) -> str:
    """Return paragraph HTML that keeps only its rerouted internal links.

    Empty when the paragraph has none, so an ordinary Premier League paragraph
    keeps taking the plain-text path it always has.
    """
    if not rerouted_links or not any(
        str(anchor.get("href") or "").strip() in rerouted_links
        for anchor in paragraph.find_all("a", href=True)
    ):
        return ""
    return _inline_paragraph_html(paragraph, rerouted_links).strip()


def _metadata_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def is_telegraph_url(url: str) -> bool:
    return (urlparse(str(url or "").strip()).hostname or "").lower() in _TELEGRAPH_PAGE_HOSTS


def reroute_internal_links(soup, base_url: str) -> set[str]:
    """Point a source article's own cross-links at our translation of them.

    These sites link mostly to their own earlier articles, and a large share of
    those already have a published Persian page in the Telegraph catalog. The
    hyperlink is therefore worth keeping as long as its destination is replaced
    by our own article — the reader stays in Persian and inside the channel's
    catalogue. Anchors whose destination has not been translated are left
    untouched here and dropped by the caller, exactly as before.

    Returns the Telegraph URLs that anchors now point at, so a caller can tell
    a rerouted link apart from a Telegraph URL that was in the source page.
    """
    # A list of pairs, not a dict: BeautifulSoup hashes a tag by its markup, so
    # an article that links to the same page twice with the same text would
    # keep only one of the two anchors.
    destinations: list[tuple] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor.get("href") or "").strip())
        if href.startswith(("http://", "https://")) and anchor.get_text(strip=True):
            destinations.append((anchor, href))
    if not destinations:
        return set()

    try:
        import article_catalog

        translated = article_catalog.resolve_source_links(
            {href for _, href in destinations}
        )
    except Exception:
        # A catalog problem must never stop an article from being published;
        # without the mapping every link is simply dropped as it used to be.
        logger.exception("Could not resolve internal article links")
        return set()

    rerouted = set()
    for anchor, href in destinations:
        target = translated.get(href, "")
        if target:
            anchor.attrs = {"href": target}
            rerouted.add(target)
    if rerouted:
        logger.info("Rerouted %d internal link(s) to published Telegraph articles", len(rerouted))
    return rerouted


def _telegraph_safe_article_html(extracted_html: str, base_url: str) -> tuple[str, list[str]]:
    """Keep reader-mode structure while restricting it to Telegraph-safe HTML."""
    soup = BeautifulSoup(extracted_html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()

    rerouted_links = reroute_internal_links(soup, base_url)
    images: list[str] = []
    for tag in soup.find_all(True):
        if tag.name in {"h1", "h2"}:
            tag.name = "h3"
        elif tag.name not in _TELEGRAPH_TAGS:
            tag.unwrap()
            continue

        if tag.name == "a":
            # Source article links are otherwise promotional or SEO links, and
            # a link out of the channel is of no use to a Persian reader. Keep
            # the visible text and drop everything except the cross-links that
            # were rerouted to our own published translation.
            href = str(tag.get("href") or "").strip()
            if href in rerouted_links:
                tag.attrs = {"href": href}
            else:
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


def _is_fff_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in _FFFIX_HOSTS and parsed.path.startswith("/blog-index/")


def _fff_normalized_text(element) -> str:
    return " ".join(element.get_text(" ", strip=True).split()).lower()


def _clean_fff_article_html(source_html: str) -> str:
    """Remove Fantasy Football Fix's inline and trailing sales blocks.

    FFFix places the article in a stable ``section.block-paragraph``. Its
    inline sales links are a list between two ``hr`` elements, while the
    trailing offer starts at the final ``hr`` and includes a banner image.
    Cleaning this before reader-mode extraction prevents promotional copy and
    images from reaching translation.
    """
    soup = BeautifulSoup(source_html, "html.parser")
    article = soup.select_one("div.white-inner.blog-article")
    content = article.select_one("section.block-paragraph") if article else None
    if content is None:
        return source_html

    # These are the three-link Premium Plus block. Match both its visible
    # labels and destinations so ordinary article lists are left untouched.
    for unordered_list in list(content.find_all("ul")):
        labels = _fff_normalized_text(unordered_list)
        links = {
            urlparse(urljoin("https://www.fantasyfootballfix.com", anchor.get("href", ""))).path
            for anchor in unordered_list.find_all("a")
        }
        if all(label in labels for label in _FFFIX_PROMO_LABELS) and _FFFIX_PROMO_LINKS <= links:
            previous = unordered_list.find_previous_sibling()
            following = unordered_list.find_next_sibling()
            unordered_list.decompose()
            if previous and previous.name == "hr":
                previous.decompose()
            if following and following.name == "hr":
                following.decompose()

    # The end-of-article cut used to happen here, keyed on FFFix's promo
    # banner filename or its offer wording. Both are brittle: when the site
    # changed either one, the heuristic either stopped trimming or, worse, cut
    # at the wrong divider and truncated the article after its introduction.
    # Trailing promotional copy is now removed by the translator instead, which
    # also reports the promo image placeholders it dropped. Keep this function
    # to structural scoping only.

    # Keep the extractor focused on the article body even if the page adds
    # related cards or a site-wide offer inside the outer article wrapper.
    for extra in article.select(".blog-tag-btns, .blog-end-offer, .blog-cards--related"):
        extra.decompose()
    # This dedicated image is the article's feature image. It is sent as the
    # Telegram media post and must not be repeated in Telegraph. The source
    # uses a WebP variant here while og:image commonly points to a PNG.
    header = article.select_one(".blog-image")
    if header:
        header.decompose()
    # Return only the cleaned article body. Passing the whole wrapper through
    # reader mode can flatten otherwise correctly ordered images to the end.
    return str(content)


def _is_ffscout_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in _FFSCOUT_HOSTS


def _clean_ffscout_article_html(source_html: str) -> str:
    """Remove FFScout's inline read-more and trailing promotion blocks."""
    soup = BeautifulSoup(source_html, "html.parser")
    entry = soup.select_one("section.entry-content")
    if entry is None:
        return source_html

    # The inline "READ MORE:" drop and the cut at the final WordPress
    # separator were removed: the separator is not reliably the end of the
    # article, so this silently truncated posts whose last section happened to
    # be separated. The translator strips read-more links and trailing site
    # promotion instead.
    return str(entry)


def _ffscout_credentials() -> tuple[str, str] | None:
    email = os.getenv("FFSCOUT_EMAIL", "").strip()
    password = os.getenv("FSCOUT_PASS", "")
    return (email, password) if email and password else None


def _fetch_ffscout_article(url: str) -> requests.Response:
    """Fetch FFScout with the optional credentials configured in .env."""
    session = requests.Session()
    session.headers.update(_HEADERS)
    response = session.get(url, timeout=20, allow_redirects=True)

    credentials = _ffscout_credentials()
    if not credentials:
        return response

    soup = BeautifulSoup(response.text, "html.parser")
    login_form = soup.find(
        "form",
        action=lambda value: value and "wp-login.php" in value,
    )
    if login_form is None:
        logger.info("FFScout login form was not present for %s", url)
        return response

    data = {}
    for field in login_form.find_all("input"):
        name = field.get("name")
        field_type = (field.get("type") or "text").lower()
        if name and field_type not in {"submit", "button"}:
            data[name] = field.get("value", "")
    data["log"], data["pwd"] = credentials
    if login_form.find("input", attrs={"name": "rememberme"}):
        data["rememberme"] = "forever"

    submit = login_form.find("input", attrs={"name": "wp-submit"})
    if submit:
        data["wp-submit"] = submit.get("value") or "Log In"
    login_url = urljoin(response.url, login_form.get("action") or "/wp-login.php")
    login_response = session.post(
        login_url,
        data=data,
        headers={"Referer": response.url},
        timeout=20,
        allow_redirects=True,
    )
    login_text = login_response.text.lower()
    login_failed = any(
        marker in login_text
        for marker in (
            "incorrect password",
            "invalid username",
            "login failed",
            "error: the password",
        )
    )
    if login_failed:
        logger.warning("FFScout login was rejected; using the public response for %s", url)
        return response

    return session.get(url, timeout=20, allow_redirects=True)


def _is_allaboutfpl_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in _ALLABOUTFPL_HOSTS


def _clean_allaboutfpl_article_html(source_html: str) -> str:
    """Remove AllAboutFPL's cross-links, affiliate promos, and footer copy."""
    soup = BeautifulSoup(source_html, "html.parser")
    entry = soup.select_one("article .entry-content, .entry-content")
    if entry is None:
        return source_html

    # The "Further reads from AllAboutFPL" cut, the "Further read:" paragraph
    # drop, and the FFHUB affiliate-block matcher all lived here. They keyed on
    # exact site wording and separator structure, so a wording change either
    # left the promo in or truncated real content. The translator removes
    # cross-links, affiliate blocks, and footer copy instead.
    return str(entry)


def fetch_general_article(url: str) -> dict | None:
    """Extract a readable, server-rendered article from an arbitrary web page."""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        if _is_ffscout_article_url(url):
            response = _fetch_ffscout_article(url)
        else:
            response = requests.get(url, headers=_HEADERS, timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
        logger.info("Could not fetch general article %s: %s", url, exc)
        return None

    if response.status_code in {401, 402, 403, 451}:
        logger.info("Skipping blocked or paywalled article %s", url)
        return None
    if not response.ok or "html" not in response.headers.get("content-type", "").lower():
        return None

    source_html = response.text
    is_fff_article = _is_fff_article_url(response.url)
    is_ffscout_article = _is_ffscout_article_url(response.url)
    is_allaboutfpl_article = _is_allaboutfpl_article_url(response.url)
    if is_fff_article:
        source_html = _clean_fff_article_html(source_html)
    elif is_ffscout_article:
        source_html = _clean_ffscout_article_html(source_html)
    elif is_allaboutfpl_article:
        source_html = _clean_allaboutfpl_article_html(source_html)

    if is_fff_article or is_ffscout_article or is_allaboutfpl_article:
        # These site-specific cleaners already isolate the article body.
        # Running the cleaned DOM through reader mode can flatten inline
        # images to the end, so retain its original order directly.
        extracted = source_html
    else:
        extracted = trafilatura.extract(
            source_html,
            url=response.url,
            output_format="html",
            include_comments=False,
            include_formatting=True,
            # Links are kept here only so ``_telegraph_safe_article_html`` can
            # see them; it drops every one that is not rerouted to one of our
            # own published articles.
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
        # Use the cleaned source when a reader-mode extractor omits images;
        # otherwise a page's promotional banners can be reintroduced by the
        # fallback even though the article cleaner removed them.
        images = _source_article_images(
            BeautifulSoup(source_html, "html.parser"), response.url
        )
    title = _metadata_content(page, "og:title", "twitter:title") or (
        page.title.get_text(" ", strip=True) if page.title else ""
    )
    if not title:
        return None
    feature_image = _metadata_content(page, "og:image", "twitter:image")
    if feature_image:
        feature_image = urljoin(response.url, feature_image)
    elif images:
        feature_image = images[0]
    return {
        "title": title,
        "summary": _metadata_content(page, "og:description", "description"),
        "url": response.url,
        "html": content_html,
        "images": images,
        "feature_image": feature_image,
        "source_name": article_source_name(response.url),
    }


def _article_image_candidates(article: dict) -> list[str]:
    candidates = list(article.get("images") or [])
    candidates.extend(
        part.get("src", "")
        for part in article.get("parts") or []
        if part.get("type") == "img"
    )
    candidates.extend(
        article.get(field, "") for field in ("feature_image", "header_image")
    )
    return list(dict.fromkeys(url for url in candidates if url))


def _drop_recurring_images(article: dict) -> dict:
    """Remove the banners this source puts on every article.

    Identified by how widely an image is reused rather than by its filename or
    its surrounding wording — see ``article_images``. A promo banner that
    survives to translation is the one thing the model cannot deal with: it
    can delete the promotional *text* around it, but the picture stays.
    """
    candidates = _article_image_candidates(article)
    if not candidates:
        return article

    try:
        import article_catalog
        import article_images

        recurring = article_images.recurring_images(
            article_catalog.source_key(article.get("url", "")), candidates
        )
    except Exception:
        # Never let image bookkeeping cost us the article.
        logger.exception("Could not check article images for recurring banners")
        return article
    if not recurring:
        return article

    surviving = [url for url in candidates if url not in recurring]
    logger.info(
        "Dropping %d recurring image(s) from %s; %d image(s) left",
        len(recurring),
        article.get("url", ""),
        len(surviving),
    )
    article["images"] = [
        url for url in article.get("images") or [] if url not in recurring
    ]
    if article.get("parts"):
        article["parts"] = [
            part for part in article["parts"]
            if not (part.get("type") == "img" and part.get("src") in recurring)
        ]
    if article.get("html"):
        article["html"] = remove_images_from_html(
            article["html"], sorted(recurring), article.get("url", "")
        )
    for field in ("feature_image", "header_image"):
        if article.get(field) in recurring:
            # Only swap when there is something to swap to. Publishing needs a
            # feature image, and a banner is better than no article.
            article[field] = surviving[0] if surviving else article[field]
    return article


def fetch_article(url: str) -> dict | None:
    """Use the site-specific extractor when available, reader mode otherwise."""
    if is_excluded_premier_league_article(url):
        logger.info("Skipping excluded Premier League article %s", url)
        return None
    if is_pl_article_url(url):
        article = _fetch_pl_article(url)
    else:
        article = fetch_general_article(url)
    return _drop_recurring_images(article) if article else article


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
            result.append(f"<p>{part.get('html') or part['text']}</p>")
        elif part["type"] == "img":
            src = part["src"]
            result.append(f'<img src="{src}">')
    return "".join(result)


def build_general_article_html(article: dict) -> str:
    """Wrap reader-mode content with a translated title."""
    return (
        f"<h3>{escape(article['title'])}</h3>"
        f"{article['html']}"
    )


def remove_images_from_html(
    html_content: str, image_urls: list[str], base_url: str = "",
) -> str:
    """Remove selected image URLs before the article is translated."""
    targets = {
        urljoin(base_url, str(url).strip())
        for url in image_urls
        if str(url).strip()
    }
    if not targets:
        return html_content
    soup = BeautifulSoup(html_content, "html.parser")
    for image in soup.find_all("img"):
        src = urljoin(base_url, str(image.get("src") or "").strip())
        if src in targets:
            image.decompose()
    root = soup.body or soup
    return "".join(str(child) for child in root.contents).strip()


def append_original_article_link(
    html_content: str, original_url: str, source_name: str = "",
) -> str:
    """Append the source link to the end of a Telegraph article only."""
    safe_url = escape(original_url, quote=True)
    source_label = (
        f"منبع اصلی مقاله در {escape(source_name)}"
        if source_name else "منبع اصلی مقاله"
    )
    return (
        f'{html_content.rstrip()}\n\n'
        f'<p><a href="{safe_url}">{source_label}</a></p>'
    )


_IMAGE_MARKER_PREFIX = "TELEADMIN_IMAGE_"
_IMAGE_MARKER_RE = re.compile(rf"\[\[{_IMAGE_MARKER_PREFIX}(\d+)\]\]")


def prepare_article_html(html_content: str) -> tuple[str, list[str]]:
    """Replace source images with stable markers before LLM translation.

    The model is free to reflow paragraphs, but each marker remains at the
    image's original position. The markers are converted back to real images
    after translation, avoiding the old append-all-images fallback.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    # Links that survived extraction were rerouted to our own Telegraph
    # articles, so they are carried through translation. Anything else is a
    # source hyperlink that must not reach the published page.
    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "").strip()
        if is_telegraph_url(href) and anchor.get_text(strip=True):
            anchor.attrs = {"href": href}
        else:
            anchor.unwrap()

    image_urls = []
    for image in soup.find_all("img"):
        src = str(image.get("src") or "").strip()
        if not src:
            image.decompose()
            continue
        index = len(image_urls) + 1
        image_urls.append(src)
        image.replace_with(NavigableString(f"[[{_IMAGE_MARKER_PREFIX}{index}]]"))

    root = soup.body or soup
    return "".join(str(child) for child in root.contents).strip(), image_urls


def reroute_html_links(html_content: str) -> tuple[str, set[str]]:
    """Apply internal-link rerouting to an already-assembled HTML fragment.

    Used by the Telegram paths, whose hyperlinks come from message entities
    rather than a scraped page. Text with no hyperlink at all is returned
    unchanged so those paths keep the exact markup they build today.
    """
    content = str(html_content or "")
    soup = BeautifulSoup(content, "html.parser")
    if not soup.find("a"):
        return content, set()

    rerouted = reroute_internal_links(soup, "")
    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "").strip()
        if href in rerouted:
            anchor.attrs = {"href": href}
        else:
            anchor.unwrap()
    root = soup.body or soup
    return "".join(str(child) for child in root.contents).strip(), rerouted


def strip_image_markers(html_content: str) -> str:
    """Remove leftover image placeholders from text that keeps no images.

    The inline-post path publishes the translation as a caption, so a marker
    the model kept for an image that was dropped would be shown to readers.
    """
    soup = BeautifulSoup(
        _IMAGE_MARKER_RE.sub("", str(html_content or "")), "html.parser"
    )
    for wrapper in list(soup.find_all(["p", "figure"])):
        if not wrapper.get_text(strip=True) and not wrapper.find("img"):
            wrapper.decompose()
    root = soup.body or soup
    return "".join(str(child) for child in root.contents).strip()


def article_link_targets(html_content: str) -> set[str]:
    """Return the rerouted Telegraph destinations present in article HTML.

    Collected before translation so the model's output can be checked against
    the links it was actually given.
    """
    soup = BeautifulSoup(str(html_content or ""), "html.parser")
    return {
        str(anchor.get("href") or "").strip()
        for anchor in soup.find_all("a", href=True)
        if is_telegraph_url(str(anchor.get("href") or ""))
    }


def sanitize_article_links(
    html_content: str, allowed_links: set[str] | None = None,
) -> str:
    """Keep only approved hyperlinks, retaining every anchor's visible text.

    ``allowed_links`` are the rerouted internal links the model was given. A
    link the model invented, moved to a different destination, or copied out of
    the source text is not in that set and is removed, so a mangled URL can
    only ever cost us the link — never publish a wrong one.
    """
    allowed = {str(url).strip() for url in (allowed_links or set())}
    soup = BeautifulSoup(str(html_content or ""), "html.parser")
    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "").strip()
        if href in allowed and anchor.get_text(strip=True):
            anchor.attrs = {"href": href}
        else:
            anchor.unwrap()
    root = soup.body or soup
    return "".join(str(child) for child in root.contents).strip()


def _image_marker_positions(html_content: str) -> tuple[dict[int, int], int]:
    """Return each marker's top-level position in the source article HTML."""
    soup = BeautifulSoup(html_content, "html.parser")
    root = soup.body or soup
    nodes = [node for node in root.contents if str(node).strip()]
    positions: dict[int, int] = {}

    for marker_node in soup.find_all(string=_IMAGE_MARKER_RE):
        current = marker_node
        while current.parent is not None and current.parent is not root:
            current = current.parent
        try:
            position = nodes.index(current)
        except ValueError:
            continue
        for match in _IMAGE_MARKER_RE.finditer(str(marker_node)):
            positions[int(match.group(1))] = position
    return positions, len(nodes)


def _restore_images_by_source_position(
    soup: BeautifulSoup, image_urls: list[str], source_html: str,
) -> set[int]:
    """Place every source image using the original markerized layout."""
    source_positions, source_node_count = _image_marker_positions(source_html)
    if not image_urls or len(source_positions) != len(image_urls):
        return set()

    root = soup.body or soup
    for image in list(soup.find_all("img")):
        src = str(image.get("src") or "").strip()
        has_marker = any(
            _IMAGE_MARKER_RE.search(str(value))
            for value in image.attrs.values()
        )
        if src in image_urls or has_marker:
            image.decompose()

    for marker_node in list(soup.find_all(string=_IMAGE_MARKER_RE)):
        text = _IMAGE_MARKER_RE.sub("", str(marker_node))
        if text:
            marker_node.replace_with(NavigableString(text))
        else:
            marker_node.extract()

    # Remove empty wrappers left when the model kept a marker-only paragraph.
    for wrapper in list(soup.find_all(["p", "figure"])):
        if not wrapper.get_text(strip=True) and not wrapper.find("img"):
            wrapper.decompose()

    target_nodes = [node for node in root.contents if str(node).strip()]
    target_node_count = len(target_nodes)
    inserted_at_rank = {}
    restored = set()

    for index, url in enumerate(image_urls, start=1):
        source_position = source_positions.get(index)
        if source_position is None:
            continue
        if not url:
            # Blanked by the caller because the translator deleted this
            # placeholder's promotional block. Count it as handled so this
            # positioning pass still applies to the remaining images.
            restored.add(index)
            continue
        image = soup.new_tag("img", src=url)
        if not target_nodes:
            root.append(image)
        else:
            target_rank = min(
                target_node_count,
                (
                    source_position * target_node_count
                    + source_node_count - 1
                ) // source_node_count,
            )
            if target_rank >= len(target_nodes):
                root.append(image)
                inserted_at_rank[target_rank] = image
            elif target_rank in inserted_at_rank:
                inserted_at_rank[target_rank].insert_after(image)
                inserted_at_rank[target_rank] = image
            else:
                target_nodes[target_rank].insert_before(image)
                inserted_at_rank[target_rank] = image
        restored.add(index)
    return restored


def restore_images_in_place(
    html_content: str,
    image_urls: list[str],
    *,
    source_html: str | None = None,
    removed_images: set[int] | None = None,
    allowed_links: set[str] | None = None,
) -> str:
    """Restore translated image markers at their original article positions.

    ``removed_images`` holds placeholder numbers the translator deleted on
    purpose because they belonged to a promotional block. They are not
    "missing" images, so they must never be re-appended at the end of the
    article — that would put the advert banner back after the content.

    ``allowed_links`` are the rerouted internal links given to the translator;
    every other hyperlink in its output is removed.
    """
    dropped = {int(index) for index in (removed_images or set())}
    if dropped:
        image_urls = [
            "" if index in dropped else url
            for index, url in enumerate(image_urls, start=1)
        ]
    soup = BeautifulSoup(
        sanitize_article_links(html_content, allowed_links), "html.parser"
    )
    if source_html:
        restored = _restore_images_by_source_position(
            soup, image_urls, source_html
        )
        if len(restored) == len(image_urls):
            root = soup.body or soup
            return "".join(str(child) for child in root.contents).strip()
    restored = set()

    def image_for_marker(index: int):
        if 0 < index <= len(image_urls) and image_urls[index - 1]:
            return soup.new_tag("img", src=image_urls[index - 1])
        return None

    # Some models understand the placeholder as an image instruction and
    # return it as ``<img src="[[TELEADMIN_IMAGE_1]]">``.  That is still a
    # valid representation of the right position, but searching only text
    # nodes (as the old implementation did) misses it and appends the image
    # at the end as a false fallback.
    for tag in list(soup.find_all(True)):
        marker_matches = []
        for value in tag.attrs.values():
            marker_matches.extend(_IMAGE_MARKER_RE.finditer(str(value)))
        if not marker_matches:
            continue

        valid_match = next(
            (
                match for match in marker_matches
                if image_for_marker(int(match.group(1))) is not None
            ),
            None,
        )
        if valid_match is None:
            continue
        index = int(valid_match.group(1))
        image = image_for_marker(index)
        if tag.name == "img":
            # Telegraph only needs the source URL. Dropping model-added attrs
            # also removes the placeholder if it was placed in alt/title.
            tag.replace_with(image)
        else:
            # Keep surrounding translated text and put the image at the
            # placeholder element's location.
            tag.insert_before(image)
            for attr, value in list(tag.attrs.items()):
                if _IMAGE_MARKER_RE.search(str(value)):
                    del tag.attrs[attr]
        restored.add(index)

    for marker_node in list(soup.find_all(string=_IMAGE_MARKER_RE)):
        text = str(marker_node)
        parent = marker_node.parent
        matches = list(_IMAGE_MARKER_RE.finditer(text))
        if len(matches) == 1 and text.strip() == matches[0].group(0):
            index = int(matches[0].group(1))
            image = image_for_marker(index)
            if image is not None:
                # Replace a marker-only block, but do not discard sibling text
                # if the model kept the marker in a paragraph with content.
                marker_block = parent.find_parent(["p", "figure"])
                if (
                    marker_block is not None
                    and marker_block.get_text(strip=True) == text.strip()
                    and not marker_block.find("img")
                ):
                    if marker_block.name == "figure":
                        marker_block.clear()
                        marker_block.append(image)
                    else:
                        marker_block.replace_with(image)
                else:
                    marker_node.replace_with(image)
                restored.add(index)
            continue

        # Markers may share a paragraph with text after the model reflows it.
        # Split the text node at each marker so the image stays at the exact
        # point where the marker occurred, rather than before the whole node.
        cursor = 0
        for match in matches:
            prefix = text[cursor:match.start()]
            if prefix:
                marker_node.insert_before(NavigableString(prefix))
            index = int(match.group(1))
            image = image_for_marker(index)
            if image is not None:
                marker_node.insert_before(image)
                restored.add(index)
            cursor = match.end()
        suffix = text[cursor:]
        if suffix:
            marker_node.insert_before(NavigableString(suffix))
        marker_node.extract()

    missing = [
        (index, url) for index, url in enumerate(image_urls, start=1)
        if index not in restored and url
    ]
    if missing and source_html:
        source_positions, source_node_count = _image_marker_positions(source_html)
        target_root = soup.body or soup
        target_nodes = [node for node in target_root.contents if str(node).strip()]
        target_node_count = len(target_nodes)
        inserted_at_rank = {}

        for index, url in missing:
            source_position = source_positions.get(index)
            if source_position is None or not source_node_count or not target_nodes:
                continue
            target_rank = min(
                target_node_count,
                (
                    source_position * target_node_count
                    + source_node_count - 1
                ) // source_node_count,
            )
            image = soup.new_tag("img", src=url)
            if target_rank >= len(target_nodes):
                target_root.append(image)
                inserted_at_rank[target_rank] = image
            elif target_rank in inserted_at_rank:
                inserted_at_rank[target_rank].insert_after(image)
                inserted_at_rank[target_rank] = image
            else:
                target_nodes[target_rank].insert_before(image)
                inserted_at_rank[target_rank] = image
            restored.add(index)

        missing = [
            (index, url) for index, url in missing if index not in restored
        ]

    if missing:
        logger.warning(
            "Article translator omitted %d image marker(s); appending %d without a source position",
            len(missing),
            len(missing),
        )
    root = soup.body or soup
    for _, url in missing:
        root.append(soup.new_tag("img", src=url))
    return "".join(str(child) for child in root.contents).strip()


def restore_missing_images(html_content: str, image_urls: list[str]) -> str:
    """Backward-compatible wrapper for callers using the old helper name."""
    return restore_images_in_place(html_content, image_urls)


def normalize_telegraph_structure(html_content: str) -> str:
    """Make generated article HTML safe for Telegraph's block layout.

    Models occasionally put prose directly between headings instead of in
    ``<p>`` elements.  Telegraph accepts those text nodes but renders them as
    one run-on block, and the editor cannot repair it until after publication.
    Normalize at the final publishing boundary so every pipeline gets the
    same guarantee.  A long, sentence-like heading is prose as well, so it is
    demoted before Telegraph turns it into an oversized heading.
    """
    soup = BeautifulSoup(str(html_content or ""), "html.parser")
    root = soup.body or soup

    block_tags = {"blockquote", "div", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "p", "pre", "section", "ul"}
    # Browser/editor HTML and model output can both produce a paragraph as a
    # child of a heading.  Telegraph treats the entire nested structure as a
    # heading unless the block is moved out first.
    for heading in list(root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])):
        anchor = heading
        for child in list(heading.find_all(recursive=False)):
            if child.name not in block_tags:
                continue
            child.extract()
            anchor.insert_after(child)
            anchor = child

    # Headings should be short labels.  This catches a model that wraps a
    # whole paragraph in h3/h4 while preserving genuine section headings.
    for heading in list(root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])):
        visible_text = heading.get_text(" ", strip=True)
        if len(visible_text) > 140 or re.search(r"[.!؟؛]\s", visible_text):
            heading.name = "p"

    # Combine consecutive root-level text nodes into a real paragraph.  Do
    # not wrap whitespace-only nodes: they are just serialization formatting.
    pending: list[str] = []

    def flush(before=None) -> None:
        nonlocal pending
        text = " ".join(part.strip() for part in pending if part.strip())
        pending = []
        if not text:
            return
        # Telegram plain-text pastes arrive here as root-level text nodes.
        # Turn their blank lines into paragraphs and their single line breaks
        # into explicit Telegraph <br> tags before whitespace collapses.
        paragraphs = []
        for block in re.split(r"\n\s*\n+", text.replace("\r\n", "\n")):
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            if not lines:
                continue
            paragraph = soup.new_tag("p")
            for index, line in enumerate(lines):
                if index:
                    paragraph.append(soup.new_tag("br"))
                paragraph.append(NavigableString(line))
            paragraphs.append(paragraph)
        if before is None:
            for paragraph in paragraphs:
                root.append(paragraph)
        else:
            # BeautifulSoup inserts before the anchor each time, so reverse
            # insertion preserves the source order.
            for paragraph in reversed(paragraphs):
                before.insert_before(paragraph)

    for child in list(root.contents):
        if isinstance(child, NavigableString):
            if child.strip():
                pending.append(str(child))
            child.extract()
            continue
        flush(child)
    flush()

    # Telegraph's own editor produces neither of the next two shapes, and a
    # page that deviates from the structure Telegram's Instant View template
    # expects is rendered as an ordinary web page instead: it loads slowly, in
    # Telegraph's own light theme, ignoring the reader's font. Both were
    # visible on published pages — a root-level <br> on every article built
    # from Telegram text, and an unwrapped <img> on every article with inline
    # images — so normalize them here, at the one boundary every page crosses.
    for line_break in list(root.find_all("br", recursive=False)):
        # The text on either side of this break is already in its own
        # paragraph, so the break itself carries nothing.
        line_break.decompose()

    for image in list(root.find_all("img")):
        if image.find_parent("figure") is None:
            image.wrap(soup.new_tag("figure"))

    # Models sometimes put all translated prose inside one <p> while keeping
    # the source's newlines. HTML rendering collapses those newlines, so make
    # them visible without disturbing inline markup. Preserve pre/code blocks.
    for node in list(root.find_all(string=True)):
        if "\n" not in str(node):
            continue
        if node.parent.name in {"pre", "code"}:
            continue
        pieces = re.split(r"\r?\n", str(node))
        replacement = NavigableString(pieces[0])
        node.replace_with(replacement)
        cursor = replacement
        for piece in pieces[1:]:
            line_break = soup.new_tag("br")
            cursor.insert_after(line_break)
            text_node = NavigableString(piece)
            line_break.insert_after(text_node)
            cursor = text_node
    return "".join(str(child) for child in root.contents).strip()


def _telegraph_slug_title(title: str) -> str:
    """Return a Latin-only temporary title used solely to create a page path."""
    text = str(title or "").strip()
    for source, replacement in _SLUG_WORDS.items():
        text = re.sub(rf"(?<!\w){re.escape(source)}(?!\w)", replacement, text)
    text = text.translate(_PERSIAN_TO_FINGLISH)
    # Telegraph derives the path from its createPage title. Keep only stable
    # Latin word characters so Persian titles never become percent-encoded.
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:180] or "FPL Article"


def publish_to_telegraph(
    title: str,
    html_content: str,
    *,
    raise_on_error: bool = False,
    summary: str = "",
    source_tag: str = "",
    image_url: str = "",
    source_url: str = "",
) -> str | None:
    try:
        tg = _get_telegraph()
        html_content = normalize_telegraph_structure(html_content)
        html_content = (
            f"{html_content.rstrip()}\n\n{_ARTICLE_CATALOGUE_FOOTER}"
        )
        page = tg.create_page(
            title=_telegraph_slug_title(title), html_content=html_content,
        )
        url = page.get("url", "")
        path = page.get("path", "")
        if path:
            # Editing changes the visible title but deliberately retains the
            # Latin path allocated by createPage.
            try:
                tg.edit_page(path=path, title=title, html_content=html_content)
            except Exception:
                # The page already exists, so preserve it and its usable URL
                # if a transient edit request fails rather than publishing a
                # duplicate on a later retry.
                logger.exception("Telegraph created %s but could not restore its visible title", url)
        else:
            logger.warning("Telegraph created a page without a path; keeping its temporary title")
        logger.info("Telegraph page created: %s", url)
        try:
            import article_catalog

            if not image_url:
                image_match = re.search(
                    r'<img\b[^>]*\bsrc=["\'](https?://[^"\']+)',
                    html_content,
                    flags=re.IGNORECASE,
                )
                image_url = image_match.group(1) if image_match else ""
            article_catalog.record_page(
                url,
                title,
                summary=summary,
                source_tag=source_tag,
                image_url=image_url,
                source_url=source_url,
            )
        except Exception:
            logger.exception("Telegraph page created but catalog indexing failed")
        return url
    except Exception as exc:
        logger.exception(
            "Telegraph publish failed: title=%r html_chars=%d html_bytes=%d error=%s",
            title,
            len(html_content),
            len(html_content.encode("utf-8")),
            exc,
        )
        if raise_on_error:
            raise TelegraphPublishError(
                f"Telegraph rejected the article: {type(exc).__name__}: {exc}"
            ) from exc
        return None
