"""Text, HTML and caption helpers for channel posts.

Nothing here needs the Telegram client, the settings or a translation
provider, so tests can import it without starting the bot."""
import asyncio
import html as html_lib
import logging
import re
from urllib.parse import urlparse

import requests

from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityTextUrl,
)

import articles
import youtube_posts


logger = logging.getLogger("TeleAdmin")


AI_SIGNATURE = "@EPL_Fantasy | \u2728AI"


# Telegram media captions allow 1,024 rendered characters.  The AI signature
# consumes 20 (including its leading line breaks); 940 leaves at least 58
# characters for a source→Persian expansion and an optional visible link label.
# HTML tags and link URLs are not counted after Telegram parses the caption.
_ARTICLE_SOURCE_THRESHOLD = 940


_YOUTUBE_DESCRIPTION_PREVIEW_LIMIT = 500


# Telegram's own limit on the caption of a media post, in UTF-16 code units.
# This, not the body limit above, is what usually decides: a caption also
# carries the title, the source link, and the AI signature.
_MEDIA_CAPTION_LIMIT = 1024


def _catalog_source_tag(event, fallback: str = "Telegram") -> str:
    """Use the source channel title as the catalog tag when available."""
    chat = getattr(event, "chat", None)
    return (
        str(getattr(chat, "title", "") or getattr(chat, "username", "")).strip()
        or fallback
    )


def _strip_quotes(text: str) -> str:
    for ch in "\u201c\u201d\u201e\u201f\u2033\u2036\"\u00ab\u00bb\u2039\u203a":
        text = text.replace(ch, "")
    return text


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _rendered_text_length(text: str) -> int:
    return len(html_lib.unescape(_strip_html_tags(text)))


def _caption_length(text: str) -> int:
    """Measure a caption the way Telegram does, in UTF-16 code units.

    An emoji costs two, and every caption here ends with the AI signature, so
    counting characters would under-report and let a caption through that
    Telegram then rejects.
    """
    rendered = html_lib.unescape(_strip_html_tags(text))
    return len(rendered.encode("utf-16-le")) // 2


def _strip_article_images(text: str) -> str:
    return re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)


def _article_title(text: str, fallback: str = "مقاله فانتزی") -> str:
    """Return Telegraph-safe plain text for a page title (never HTML)."""
    heading = re.search(r"<h[1-4][^>]*>(.*?)</h[1-4]>", text, re.IGNORECASE | re.DOTALL)
    title_source = heading.group(1) if heading else text
    title = html_lib.unescape(_strip_html_tags(title_source)).strip()
    if not title:
        title = html_lib.unescape(_strip_html_tags(fallback)).strip()
    return title[:256] or "مقاله فانتزی"


def _telegraph_to_telegram_html(text: str) -> str:
    """Convert Telegraph article structure into Telegram caption-safe HTML."""
    text = re.sub(r"<h[34][^>]*>", "<b>", text, flags=re.IGNORECASE)
    text = re.sub(r"</h[34]>", "</b>\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:ul|ol|figure|figcaption)[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _youtube_description_preview(text: str) -> str:
    """Return a caption-safe, plain-text preview of a translated description."""
    text = html_lib.unescape(_strip_html_tags(text)).strip()
    if len(text) > _YOUTUBE_DESCRIPTION_PREVIEW_LIMIT:
        text = text[:_YOUTUBE_DESCRIPTION_PREVIEW_LIMIT - 1].rstrip() + "…"
    return _escape_html(text)


def _youtube_thumbnail_url(url: str) -> str:
    """Return a stable public thumbnail URL for a YouTube page."""
    try:
        video_id = youtube_posts.extract_video_id(url)
    except youtube_posts.YouTubeImportError:
        return ""
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _format_telegraph_post(
    title: str, summary: str, telegraph_url: str,
) -> str:
    summary_text = _strip_visible_urls(html_lib.unescape(_strip_html_tags(summary)))
    post = (
        f"<b>{_escape_html(title)}</b>\n\n"
        f"- - - - - - - - -\n\n"
        f"{_escape_html(summary_text)}"
    )
    post += (
        f'\n\n<b><a href="{_escape_html(telegraph_url)}">'
        f"👈👈متن کامل فارسی مقاله👉👉</a></b>"
    )
    return f"{post}\n\n{AI_SIGNATURE}"


def _format_short_article_post(
    title: str, body: str, original_url: str, *, source_name: str = "",
) -> str:
    body = _strip_visible_urls(
        _telegraph_to_telegram_html(_strip_article_images(body)), html=True
    )
    source_label = (
        f"منبع اصلی مقاله در {_escape_html(source_name)}"
        if source_name else "منبع اصلی مقاله"
    )
    parts = [
        f"<b>{_escape_html(title)}</b>",
        body,
        f'<a href="{_escape_html(original_url)}">{source_label}</a>',
        AI_SIGNATURE,
    ]
    return "\n\n".join(part for part in parts if part)


# The thumbnail already shows whose video it is, so the posts open with the
# title rather than with a "new video from <channel>" line that repeated it.
# The summary is written in the creator's own voice, so the post names them
# right under the title; otherwise "I" would read as the channel admin.
def _youtube_byline(creator: str) -> str:
    return f"\n🎙 {_escape_html(creator)}" if creator else ""


def _format_youtube_telegraph_post(
    title: str, summary: str, telegraph_url: str, original_url: str,
    *, creator: str = "",
) -> str:
    return (
        f"<b>{_escape_html(title)}</b>{_youtube_byline(creator)}\n\n"
        f"- - - - - - - - -\n\n"
        f"{summary}\n\n"
        f'<a href="{_escape_html(original_url)}">مشاهدهٔ ویدیوی اصلی در YouTube</a>\n\n'
        f'<b><a href="{_escape_html(telegraph_url)}">👈👈متن کامل فارسی ویدئو👉👉</a></b>\n\n'
        f"{AI_SIGNATURE}"
    )


def _format_youtube_inline_post(
    title: str, transcript: str, original_url: str, *, creator: str = "",
) -> str:
    return (
        f"<b>{title}</b>{_youtube_byline(creator)}\n\n"
        f"{transcript}\n\n"
        f'<a href="{_escape_html(original_url)}">مشاهدهٔ ویدیوی اصلی در YouTube</a>\n\n'
        f"{AI_SIGNATURE}"
    )


def _message_to_html(text: str, entities: list | None) -> str:
    if not entities:
        return _escape_html(text)

    offsets: list[tuple[int, str]] = []
    for e in entities:
        entity_type = type(e)
        if entity_type is MessageEntityBlockquote:
            offsets.append((e.offset, "<blockquote>"))
            offsets.append((e.offset + e.length, "</blockquote>"))
        elif entity_type is MessageEntityTextUrl:
            tag = f'<a href="{_escape_html(e.url)}">'
            offsets.append((e.offset, tag))
            offsets.append((e.offset + e.length, "</a>"))

    if not offsets:
        return _escape_html(text)

    offsets.sort(key=lambda x: (x[0], x[1]))
    result = []
    pos = 0
    for offset, tag in offsets:
        if offset > pos:
            result.append(_escape_html(text[pos:offset]))
        result.append(tag)
        pos = offset
    if pos < len(text):
        result.append(_escape_html(text[pos:]))
    return "".join(result)


def _fix_unclosed_tags(html: str) -> str:
    depth = 0
    result = []
    i = 0
    while i < len(html):
        if html[i:i+12] == "<blockquote>":
            depth += 1
            result.append("<blockquote>")
            i += 12
        elif html[i:i+13] == "</blockquote>":
            depth -= 1
            result.append("</blockquote>")
            i += 13
        else:
            result.append(html[i])
            i += 1
    while depth > 0:
        result.append("</blockquote>")
        depth -= 1
    return "".join(result)


def _prepare_plain_article_layout(html: str) -> str:
    """Make line breaks in pasted plain text explicit before article translation."""
    if not html or re.search(r"<(?:blockquote|a)\b", html, flags=re.IGNORECASE):
        return html
    return html.replace("\r\n", "\n").replace("\n", "<br>")


def _prepare_article_source(html: str) -> tuple[str, set[str]]:
    """Ready a Telegram post for the article translator.

    Rerouting runs first: a post's hyperlinks either become links to our own
    published translation or disappear, and only once they are gone can the
    plain-text layout pass see the post for what it is and make its line breaks
    explicit.
    """
    rerouted, allowed_links = articles.reroute_html_links(html)
    return _prepare_plain_article_layout(rerouted), allowed_links


_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩"


_ENGLISH_DIGITS = "01234567890123456789"


_DIGIT_TRANS = str.maketrans(_PERSIAN_DIGITS, _ENGLISH_DIGITS)


_HASHTAG_RE = re.compile(r"(^|\s)#(?=\w)")


_URL_RE = re.compile(
    r"(?:https?://|t\.me/|premierleague(?:-alternate)?\.app\.link/|"
    r"preml\.ge/|(?:www\.)?premierleague\.com/(?:en/)?news/)\S+", re.IGNORECASE,
)


_TCO_URL_RE = re.compile(r"https?://t\.co/\S+", re.IGNORECASE)


_TRAILING_URL_PUNCTUATION = ".,!?;:)]}»”"


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_numbers(text: str) -> str:
    parts = re.split(r"(\d+)", text.translate(_DIGIT_TRANS))
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append(f"<b>{part}</b>")
        else:
            result.append(_escape_html(part))
    return "".join(result)


def _strip_hashtags(text: str) -> str:
    lines = text.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        tokens = stripped.split()
        if tokens and all(t.startswith("#") for t in tokens):
            continue
        result.append(_HASHTAG_RE.sub(r"\1", line))
    return "\n".join(result)


def _extract_urls(event) -> list[str]:
    return _extract_urls_from_message(event.message)


def _extract_urls_from_message(message) -> list[str]:
    """Return raw and entity-backed URLs from any Telegram message object."""
    urls = []
    text = getattr(message, "text", None) or getattr(message, "message", None) or ""
    if text:
        urls.extend(
            url if url.lower().startswith(("http://", "https://")) else "https://" + url
            for m in _URL_RE.finditer(text) for url in [m.group(0)]
        )
    entities = getattr(message, "entities", None)
    if entities:
        for entity in entities:
            url = getattr(entity, "url", None)
            if url:
                if not url.lower().startswith(("http://", "https://")) and _URL_RE.match(url):
                    url = "https://" + url
                urls.append(url)
    return urls


def _canonical_link_url(url: str) -> str:
    """Expand share links to their canonical public page URLs."""
    url = url.rstrip(_TRAILING_URL_PUNCTUATION)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in articles._PL_APP_LINK_HOSTS:
        try:
            return articles.resolve_pl_share_url(url) or url
        except requests.RequestException as exc:
            logger.warning("Could not resolve PL share URL %s: %s", url, exc)
            return url
    if host == "youtu.be":
        try:
            return f"https://www.youtube.com/watch?v={youtube_posts.extract_video_id(url)}"
        except youtube_posts.YouTubeImportError:
            return url
    if host != "t.co":
        return url

    try:
        # t.co is X's redirector. HEAD avoids downloading the destination page;
        # a small streamed GET covers destinations that do not accept HEAD.
        response = requests.head(url, allow_redirects=True, timeout=15)
        if response.status_code >= 400 or response.url == url:
            response.close()
            response = requests.get(url, allow_redirects=True, stream=True, timeout=15)
        final_url = response.url.rstrip(_TRAILING_URL_PUNCTUATION)
        response.close()
    except requests.RequestException as exc:
        logger.warning("Could not expand t.co URL %s: %s", url, exc)
        return url
    return _canonical_link_url(final_url) if final_url != url else url


async def _display_link_url(urls: list[str]) -> str | None:
    if not urls:
        return None
    return await asyncio.to_thread(_canonical_link_url, urls[0])


def _expand_x_short_links(text: str) -> str:
    return _TCO_URL_RE.sub(lambda match: _canonical_link_url(match.group(0)), text)


def _strip_visible_urls(text: str, *, html: bool = False) -> str:
    """Remove printed URLs without touching HTML tags or href attributes.

    Matching visible text also handles an escaped URL's `&amp;` query string
    and a model echoing the resolved destination rather than the source URL.
    """
    parts = re.split(r"(<[^>]+>)", text) if html else [text]
    for index in range(0, len(parts), 2):
        parts[index] = _URL_RE.sub("", parts[index])
    cleaned = "".join(parts)
    if html:
        # A hidden link may use the URL itself as its visible label. Once
        # that label is removed, discard the empty anchor as well.
        cleaned = re.sub(r"<a\b[^>]*>\s*</a>", "", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", cleaned).strip()


def _build_caption(
    translated: str | None, *, link_url: str | None = None, html: bool = False,
    link_label: str = "لینک",
) -> str:
    parts = []
    if translated:
        translated = _strip_visible_urls(translated, html=html)
        if translated:
            parts.append(translated if html else _format_numbers(translated))
    if link_url:
        parts.append(f'<a href="{_escape_html(link_url)}">{link_label}</a>')
    if not parts:
        return AI_SIGNATURE
    return "\n\n".join(parts + [AI_SIGNATURE])


def _media_suffix(event) -> str:
    if event.message.file and event.message.file.ext:
        extension = event.message.file.ext
        return extension if extension.startswith(".") else f".{extension}"
    return ""


def _has_uploadable_media(event) -> bool:
    """Return whether a message contains a file Telegram can re-upload.

    Link previews populate ``message.media`` as well, but they are webpage
    metadata rather than a photo or document. Downloading one leaves an empty
    temporary file, which Telegram rejects with ``SendMediaRequest``.
    """
    return bool(event.message.photo or event.message.document)


_X_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_-]+)")


def _x_text_to_html(text: str) -> str:
    """Escape X text while making mentions point back to the X profile."""
    result = []
    pos = 0
    for match in _X_MENTION_RE.finditer(text):
        result.append(_escape_html(text[pos:match.start()]))
        handle = match.group(1)
        result.append(f'<a href="https://x.com/{handle}">{handle}</a>')
        pos = match.end()
    result.append(_escape_html(text[pos:]))
    return "".join(result)
