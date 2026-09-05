import asyncio
import html as html_lib
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import (
    Channel,
    MessageEntityBlockquote,
    MessageEntityTextUrl,
    User,
)

from config import load_config
from translator import Translator, TranslationError, ContentIncompleteError, VIDEO_TITLE_INSTRUCTIONS
import alerts
import price_changes
import deadlines
import articles
import article_monitor
import database as db
import post_queue
import scheduler
import telegraph_editor
import runtime_config
import x_posts
import youtube_posts
import youtube_monitor
import livefpl
import player_names
from admin_dashboard import AdminDashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("TeleAdmin")

settings = load_config()
runtime_config.init()
# Ensure the SQLite schema exists before any handler or scheduler task
# touches the database.
db.init_db()

translator = Translator(
    api_key=settings.openrouter_api_key,
    model=settings.openrouter_model,
    fallback_model=settings.fallback_model,
    google_api_key=settings.google_aistudio_key,
    google_model=settings.google_aistudio_model,
)

_session_string = os.getenv("TELETHON_SESSION_STRING")
client = TelegramClient(
    StringSession(_session_string) if _session_string else "translation_session",
    settings.telegram_api_id,
    settings.telegram_api_hash,
)

SIGNATURE = "@EPL_Fantasy"
AI_SIGNATURE = "@EPL_Fantasy | \u2728AI"
QUEUED_POST_CONFIRMATION = "در اولین بازهٔ خالی زمان‌بندی کانال قرار گرفت."
_ALBUM_TIMEOUT = 5
# Telegram media captions allow 1,024 rendered characters.  The AI signature
# consumes 20 (including its leading line breaks); 940 leaves at least 58
# characters for a source→Persian expansion and an optional visible link label.
# HTML tags and link URLs are not counted after Telegram parses the caption.
_ARTICLE_SOURCE_THRESHOLD = 940
# Media captions are limited to 1,024 rendered characters. Keep the short
# article heuristic conservative because Persian translation can expand and
# the caption also contains the article header, source link, and signature.
# Not everything needs a Telegraph page. A translation that fits in a caption,
# with no inline image to place, is published as an ordinary post carrying its
# feature image. There is no separate length limit on the body: the caption
# either holds the whole thing or the article goes to Telegraph, so nothing is
# ever trimmed to make it fit.
_CHUNK_TIMEOUT = 3  # seconds to wait for text chunks from same chat
# A quiet window is the only thing holding a forwarded burst together, so it has
# to outlast the slowest post in it. Three seconds was short enough that a post
# arriving behind the others — in practice one carrying a photo — missed the
# batch and was published on its own. Once the batch contains media it waits
# longer still, because that is when arrival is most uneven.
_FORWARDED_BATCH_TIMEOUT = 6
_FORWARDED_BATCH_MEDIA_TIMEOUT = 12
_YOUTUBE_DESCRIPTION_PREVIEW_LIMIT = 500
_CATALOG_VISIBILITY_LOOKBACK = timedelta(days=3)
_CATALOG_ENRICHMENT_BATCH = 25
# One start's worth of backlog. Each page costs a Telegraph fetch, and pages
# that recover nothing are stamped so the next start continues past them.
_CATALOG_ENRICHMENT_LIMIT = 500
_CATALOG_ENRICHMENT_DELAY = 0.5
# Telegram's own limit on the caption of a media post, in UTF-16 code units.
# This, not the body limit above, is what usually decides: a caption also
# carries the title, the source link, and the AI signature.
_MEDIA_CAPTION_LIMIT = 1024
_AUTOMATIC_ONLY_SOURCE_REFS = ("@FPLFootball",)
# A match plus stoppage, a delayed start, and the wait before the API marks it
# finished. A fixture unfinished for longer than this was postponed.
_FIXTURE_IN_PROGRESS_HOURS = 4

_album_buffer: dict[int, list] = {}
_album_tasks: dict[int, asyncio.Task] = {}
_album_caption: dict[int, str] = {}

_chunk_buffer: dict[int, list] = {}
_chunk_tasks: dict[int, asyncio.Task] = {}
_forward_batch_buffer: dict[int, list] = {}
_forward_batch_tasks: dict[int, asyncio.Task] = {}
_forward_batch_waiters: dict[int, list[asyncio.Future]] = {}
_forward_batch_deadlines: dict[int, float] = {}
_pending_dashboard_content: str | None = None
_admin_bot_client: TelegramClient | None = None
_schedule_slot_lock = asyncio.Lock()
# Telegram can briefly omit a message immediately after it is scheduled. Keep
# the last allocated slot locally so successive posts advance even when the
# scheduled-message history has not caught up yet. The history remains the
# restart-safe source of truth because this cursor only lives in memory.
_last_reserved_queue_slots: dict[str, datetime] = {}
_youtube_failure_notified: set[str] = set()
_youtube_incomplete_attempts: dict[str, int] = {}
# Captions are often still being generated shortly after upload, so a partial
# transcript is retried across polls before the video is skipped.
_MAX_INCOMPLETE_TRANSCRIPT_ATTEMPTS = 4
_automatic_alert_lock = asyncio.Lock()


def _target_channel() -> str:
    return runtime_config.get("TARGET_CHANNEL_ID")


def _catalog_source_tag(event, fallback: str = "Telegram") -> str:
    """Use the source channel title as the catalog tag when available."""
    chat = getattr(event, "chat", None)
    return (
        str(getattr(chat, "title", "") or getattr(chat, "username", "")).strip()
        or fallback
    )


def _telegram_source_list() -> str:
    sources = runtime_config.telegram_sources()
    if not sources:
        return "هیچ منبع تلگرامی به‌عنوان منبع ثبت نشده است."
    rows = [
        f"<blockquote><b>{_escape_html(source['title'])}</b>\n"
        f"<code>{_escape_html(source['source_ref'])}</code>"
        + ("\nفقط هشدار و ترکیب خودکار" if source.get("automatic_only") else "")
        + "</blockquote>"
        for source in sources
    ]
    return "<b>📡 منابع تلگرام</b>\n\n" + "\n".join(rows)


def _normalise_source_reference(value: str) -> str:
    value = value.strip()
    if value.startswith("@") or value.lstrip("-").isdigit():
        return value
    parsed = urlparse(value if "://" in value else f"https://t.me/{value.lstrip('@')}")
    if (parsed.hostname or "").lower() in {"t.me", "www.t.me", "telegram.me"}:
        part = parsed.path.strip("/").split("/", 1)[0]
        if part and not part.startswith("+"):
            return f"@{part}"
    raise ValueError("شناسهٔ کانال را به شکل @channel یا لینک t.me ارسال کنید.")


async def _resolve_telegram_source(value: str) -> tuple[int, str, str]:
    reference = _normalise_source_reference(value)
    try:
        entity = await client.get_entity(reference)
    except Exception as exc:
        raise ValueError("کانال پیدا نشد؛ مطمئن شوید حساب TeleAdmin به آن دسترسی دارد.") from exc
    is_broadcast_channel = isinstance(entity, Channel) and entity.broadcast
    is_alert_bot = isinstance(entity, User) and entity.bot
    if not (is_broadcast_channel or is_alert_bot):
        raise ValueError("فقط کانال‌های پخش یا ربات‌های هشدار می‌توانند منبع باشند.")
    title = (
        getattr(entity, "title", None)
        or getattr(entity, "first_name", None)
        or getattr(entity, "username", None)
        or str(entity.id)
    ).strip()
    source_ref = f"@{entity.username}" if entity.username else str(entity.id)
    return entity.id, title, source_ref


async def _add_telegram_source(value: str, actor_id: int) -> str:
    channel_id, title, source_ref = await _resolve_telegram_source(value)
    automatic_only = source_ref.casefold() in {
        value.casefold() for value in _AUTOMATIC_ONLY_SOURCE_REFS
    }
    if not runtime_config.add_telegram_source(
        channel_id, title, source_ref, actor_id, automatic_only=automatic_only
    ):
        return f"<b>{_escape_html(title)}</b> از قبل در فهرست منابع است."
    mode = " (فقط هشدار و ترکیب خودکار)" if automatic_only else ""
    return f"✅ منبع <b>{_escape_html(title)}</b>{mode} به فهرست منابع اضافه شد."


async def _remove_telegram_source(value: str, actor_id: int) -> str:
    reference = _normalise_source_reference(value)
    try:
        channel_id, title, _ = await _resolve_telegram_source(reference)
    except ValueError:
        stored = runtime_config.telegram_source_by_reference(reference)
        if not stored:
            raise
        channel_id = stored["channel_id"]
        title = stored["title"]
    removed = runtime_config.remove_telegram_source(channel_id, actor_id)
    if not removed:
        return f"<b>{_escape_html(title)}</b> در فهرست منابع نیست."
    return f"✅ منبع <b>{_escape_html(removed)}</b> از فهرست منابع حذف شد."


async def _handle_configured_source_message(event) -> None:
    peer = getattr(event.message, "peer_id", None)
    source_id = getattr(peer, "channel_id", None) or getattr(peer, "user_id", None)
    if source_id is None or not runtime_config.is_telegram_source(source_id):
        return
    await handle_new_message(
        event,
        automatic_only=runtime_config.telegram_source_is_automatic_only(source_id),
    )


async def _ensure_automatic_sources() -> None:
    """Register built-in contingency feeds after the user session is ready."""
    for source_ref in _AUTOMATIC_ONLY_SOURCE_REFS:
        try:
            channel_id, title, resolved_ref = await _resolve_telegram_source(source_ref)
            runtime_config.add_telegram_source(
                channel_id,
                title,
                resolved_ref,
                actor_id=0,
                automatic_only=True,
            )
            logger.info("Automatic-only Telegram source ready: %s (%s)", title, resolved_ref)
        except Exception as exc:
            logger.warning("Could not register automatic-only source %s: %s", source_ref, exc)


def _refresh_translator_model() -> None:
    translator.openrouter_model = runtime_config.get("OPEN_ROUTER_MODEL")


def _get_reply_to(event) -> int | None:
    if not event.message.reply_to:
        return None
    reply_msg_id = event.message.reply_to.reply_to_msg_id
    if not reply_msg_id:
        return None
    return db.lookup_target_msg(event.chat_id, reply_msg_id)


def _save_mapping(event, target_msg_id: int) -> None:
    db.store_message_mapping(event.chat_id, event.message.id, target_msg_id)


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
    summary_text = html_lib.unescape(_strip_html_tags(summary)).strip()
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
    body = _telegraph_to_telegram_html(_strip_article_images(body))
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
def _format_youtube_telegraph_post(
    title: str, summary: str, telegraph_url: str, original_url: str,
) -> str:
    return (
        f"<b>{_escape_html(title)}</b>\n\n"
        f"- - - - - - - - -\n\n"
        f"{summary}\n\n"
        f'<a href="{_escape_html(original_url)}">مشاهدهٔ ویدیوی اصلی در YouTube</a>\n\n'
        f'<b><a href="{_escape_html(telegraph_url)}">👈👈متن کامل فارسی ویدئو👉👉</a></b>\n\n'
        f"{AI_SIGNATURE}"
    )


def _format_youtube_inline_post(
    title: str, transcript: str, original_url: str,
) -> str:
    return (
        f"<b>{title}</b>\n\n"
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
_URL_RE = re.compile(r"(?:https?://|t\.me/)\S+")
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
        urls.extend(m.group(0) for m in _URL_RE.finditer(text))
    entities = getattr(message, "entities", None)
    if entities:
        for entity in entities:
            url = getattr(entity, "url", None)
            if url:
                urls.append(url)
    return urls


def _channel_key(value) -> str:
    """Normalize Telegram's bare and -100-prefixed channel identifiers."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lstrip("-").isdigit():
        text = str(abs(int(text)))
        return text[3:] if text.startswith("100") else text
    return text.lstrip("@").lower()


async def _is_target_channel_message(event) -> bool:
    target = _target_channel()
    if not target:
        return False
    target_key = _channel_key(target)
    channel_id = getattr(getattr(event.message, "peer_id", None), "channel_id", None)
    if channel_id is not None and _channel_key(channel_id) == target_key:
        return True
    if str(target).lstrip("-").isdigit():
        return False
    chat = await event.get_chat()
    return _channel_key(getattr(chat, "username", "")) == target_key


async def _publish_channel_article_links(message) -> int:
    """Expose indexed Telegraph pages whose links are in a channel message."""
    import article_catalog

    published = 0
    since = datetime.now(timezone.utc) - _CATALOG_VISIBILITY_LOOKBACK
    for url in _extract_urls_from_message(message):
        host = (urlparse(url).hostname or "").lower()
        if host not in {"telegra.ph", "www.telegra.ph", "graph.org", "www.graph.org"}:
            continue
        if await asyncio.to_thread(article_catalog.publish_in_catalog, url, since=since):
            published += 1
    return published


async def _handle_target_channel_message(event) -> None:
    """Synchronize catalog visibility with posts actually present in target."""
    if not await _is_target_channel_message(event):
        return
    published = await _publish_channel_article_links(event.message)
    if published:
        logger.info("Made %d article(s) visible from target-channel post", published)


async def _sync_catalog_visibility_from_target() -> None:
    """Catch up with already-published target posts after a restart."""
    target = _target_channel()
    if not target:
        return
    try:
        # Populate the local index first. This matters after a fresh deploy
        # where the database is empty but the Telegraph account has history.
        import article_catalog
        await asyncio.to_thread(article_catalog.sync_from_telegraph)
        published = 0
        cutoff = datetime.now(timezone.utc) - _CATALOG_VISIBILITY_LOOKBACK
        async for message in client.iter_messages(target):
            message_date = getattr(message, "date", None)
            if message_date is not None and message_date.astimezone(timezone.utc) < cutoff:
                break
            published += await _publish_channel_article_links(message)
        if published:
            logger.info("Made %d article(s) visible from target-channel history", published)
    except Exception:
        logger.exception("Could not read target channel for catalog visibility sync")


def _canonical_link_url(url: str) -> str:
    """Expand X's t.co link and use a canonical watch URL for youtu.be links."""
    url = url.rstrip(_TRAILING_URL_PUNCTUATION)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
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


def _clean_text(text: str, event=None) -> tuple[str | None, str | None]:
    text = _strip_hashtags(text)
    urls = _extract_urls(event) if event else []
    link_url = urls[0] if urls else None
    for url in urls:
        text = text.replace(url, "")
    text = text.strip() or None
    return text, link_url


def _build_caption(
    translated: str | None, *, link_url: str | None = None, html: bool = False,
    link_label: str = "لینک",
) -> str:
    parts = []
    if translated:
        if html:
            parts.append(translated)
        else:
            parts.append(_format_numbers(translated))
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


async def _send_notification(event, caption: str, *, source: str | None = None, is_media: bool | None = None):
    if source is None:
        source = (
            getattr(event.chat, "title", None)
            or getattr(event.chat, "username", None)
            or str(event.chat_id)
        )

    preview = caption
    if len(preview) > 300:
        preview = preview[:300] + "..."

    media_tag = "Media" if (event.message.media if is_media is None else is_media) else "Text"
    notif = (
        f"<b>[{media_tag}] New post</b>\n"
        f"<b>Source:</b> {source}\n\n"
        f"{preview}"
    )

    # Notifications are delivered directly through the private dashboard bot.
    if _admin_bot_client:
        for admin_id in settings.admin_user_ids:
            try:
                await _admin_bot_client.send_message(admin_id, notif, parse_mode="html")
            except Exception as exc:
                logger.warning("Could not notify admin %s: %s", admin_id, exc)


async def _notify_youtube_monitor_failure(video, error: Exception):
    """Alert admins once when a monitored video cannot be published."""
    if isinstance(error, ContentIncompleteError):
        # A partial transcript is usually a provider that returned only the
        # opening minutes. Retry a few polls before giving up, so the video is
        # not dropped over one bad fetch.
        attempts = _youtube_incomplete_attempts.get(video.id, 0) + 1
        _youtube_incomplete_attempts[video.id] = attempts
        if attempts < _MAX_INCOMPLETE_TRANSCRIPT_ATTEMPTS:
            logger.info(
                "Transcript for %s looks incomplete (attempt %d/%d); will retry: %s",
                video.id, attempts, _MAX_INCOMPLETE_TRANSCRIPT_ATTEMPTS, error,
            )
            return
        if video.id in _youtube_failure_notified:
            return
        _youtube_failure_notified.add(video.id)
        # Stop the poller from re-fetching this video indefinitely.
        await asyncio.to_thread(
            runtime_config.mark_youtube_video, video.id, video.channel_id, "skipped"
        )
        caption = (
            "<b>⚠️ زیرنویس این ویدیو ناقص بود و منتشر نشد</b>\n\n"
            f'<a href="{_escape_html(video.url)}">مشاهدهٔ ویدیو</a>\n\n'
            f"{_escape_html(str(error))}"
        )
        await _send_notification(
            None, caption, source="YouTube transcription", is_media=False
        )
        return
    if not isinstance(error, youtube_posts.TranscriptProvidersExhausted):
        return
    if video.id in _youtube_failure_notified:
        return
    _youtube_failure_notified.add(video.id)
    caption = (
        "<b>❌ همهٔ سرویس‌های زیرنویس YouTube شکست خوردند</b>\n\n"
        f'<a href="{_escape_html(video.url)}">مشاهدهٔ ویدیو</a>\n\n'
        f"{_escape_html(str(error))}"
    )
    await _send_notification(
        None, caption, source="YouTube transcription", is_media=False
    )


async def _notify_article_abandoned(url: str, error: Exception):
    """Alert admins when a monitored article stayed incomplete after retries."""
    caption = (
        "<b>⚠️ این مقاله ناقص دریافت شد و منتشر نشد</b>\n\n"
        f'<a href="{_escape_html(url)}">مشاهدهٔ مقاله</a>\n\n'
        f"{_escape_html(str(error))}"
    )
    await _send_notification(None, caption, source="Article", is_media=False)


async def _next_queue_slot(target_channel: str) -> datetime:
    scheduled = await client.get_messages(target_channel, limit=100, scheduled=True)
    occupied = [message.date for message in scheduled if getattr(message, "date", None)]
    target_key = str(target_channel)
    now = datetime.now(tz=timezone.utc)
    last_reserved = _last_reserved_queue_slots.get(target_key)
    if last_reserved is not None:
        last_key = post_queue._slot_key(last_reserved)
        if any(
            value.tzinfo is not None and post_queue._slot_key(value) == last_key
            for value in occupied
        ):
            _last_reserved_queue_slots.pop(target_key, None)
    slot = post_queue.next_available_slot(
        now,
        occupied,
        after=_last_reserved_queue_slots.get(target_key),
    )
    _last_reserved_queue_slots[target_key] = slot
    return slot


async def _send_to_target(
    text: str, *, event=None, file_path=None, album_paths=None,
    queue: bool = False,
):
    target_channel = _target_channel()
    if not target_channel:
        raise RuntimeError("TARGET_CHANNEL_ID is not configured")
    reply_to = _get_reply_to(event) if event else None
    slot_guard = _schedule_slot_lock if queue else _NullAsyncLock()
    async with slot_guard:
        schedule_time = await _next_queue_slot(target_channel) if queue else None
        if schedule_time:
            logger.info(
                "Reserved translated-post slot %s Iran time",
                schedule_time.astimezone(post_queue.IRAN_TZ).strftime("%Y-%m-%d %H:%M"),
            )
        try:
            return await _send_to_target_at(
                target_channel, text, reply_to, schedule_time,
                event=event, file_path=file_path, album_paths=album_paths,
            )
        except Exception:
            if schedule_time:
                target_key = str(target_channel)
                if _last_reserved_queue_slots.get(target_key) == schedule_time:
                    _last_reserved_queue_slots.pop(target_key, None)
            raise


class _NullAsyncLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


async def _send_to_target_at(
    target_channel, text, reply_to, schedule_time, *,
    event=None, file_path=None, album_paths=None,
):
    try:
        if album_paths:
            msg = await client.send_file(
                target_channel,
                album_paths,
                caption=text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        elif file_path:
            msg = await client.send_file(
                target_channel,
                file_path,
                caption=text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        else:
            msg = await client.send_message(
                target_channel,
                text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        if event:
            mapped_message = msg[0] if isinstance(msg, (list, tuple)) else msg
            _save_mapping(event, mapped_message.id)
        return msg
    except FloodWaitError as e:
        logger.warning("FloodWaitError: sleeping %ss", e.seconds)
        await asyncio.sleep(e.seconds)
        if schedule_time:
            schedule_time = await _next_queue_slot(target_channel)
            logger.info(
                "Moved translated post after FloodWait to %s Iran time",
                schedule_time.astimezone(post_queue.IRAN_TZ).strftime("%Y-%m-%d %H:%M"),
            )
        if album_paths:
            msg = await client.send_file(
                target_channel,
                album_paths,
                caption=text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        elif file_path:
            msg = await client.send_file(
                target_channel,
                file_path,
                caption=text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        else:
            msg = await client.send_message(
                target_channel,
                text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        if event:
            mapped_message = msg[0] if isinstance(msg, (list, tuple)) else msg
            _save_mapping(event, mapped_message.id)
        return msg


def _alert_seen(key: str) -> bool:
    return db.query_scalar("SELECT value FROM last_updated WHERE key = ?", (key,)) is not None


def _mark_alert_seen(key: str) -> None:
    with db._connect() as conn:
        db._set_updated(conn, key)


async def _send_alert(farsi_text: str, event, *, dedup_key: str | None = None):
    await _send_to_target(farsi_text, event=event)
    if dedup_key:
        _mark_alert_seen(dedup_key)
    logger.info("Sent game alert to %s", _target_channel())
    await _send_notification(event, farsi_text)


async def _try_handle_automatic_content(text: str, event) -> bool:
    """Format recognised FPL source posts without an LLM or scheduling delay."""
    if alerts.is_game_alert(text):
        parsed = alerts.parse(text)
        if parsed:
            farsi = alerts.format_farsi(parsed)
            if farsi:
                alert_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                key = f"automatic_game_alert_{alert_day}_{alerts.dedup_key(parsed)}"
                async with _automatic_alert_lock:
                    if _alert_seen(key):
                        logger.info("Skipping duplicate game alert from a later source message")
                        return True
                    logger.info("Detected game-action alert, formatting directly")
                    await _send_alert(farsi, event, dedup_key=key)
                return True

    if alerts.is_lineup(text):
        parsed = alerts.parse_lineup(text)
        if not parsed:
            logger.warning("Detected lineup with an unsupported format; ignoring it")
            return True
        message_date = getattr(getattr(event, "message", None), "date", None)
        day = (
            message_date.astimezone(timezone.utc).strftime("%Y-%m-%d")
            if message_date and message_date.tzinfo
            else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        dedup_key = alerts.lineup_dedup_key(parsed, day)
        async with _automatic_alert_lock:
            if _alert_seen(dedup_key):
                logger.info("Skipping duplicate lineup from another source")
                return True
            farsi = alerts.format_lineup(parsed)
            if farsi:
                logger.info("Detected lineup, formatting directly")
                await _send_alert(farsi, event, dedup_key=dedup_key)
        return True

    if price_changes.is_price_change(text):
        logger.info(
            "Ignoring source-channel price-change post; the nightly official FPL report is authoritative"
        )
        return True

    return False


async def _forward_message(caption: str, event):
    if _has_uploadable_media(event):
        await _forward_media(caption, event)
    else:
        await _send_to_target(caption, event=event, queue=True)


async def _forward_media(caption: str, event):
    if _has_uploadable_media(event):
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=_media_suffix(event))
        try:
            temp.close()
            await event.message.download_media(file=temp.name)
            await _send_to_target(caption, file_path=temp.name, event=event, queue=True)
        finally:
            os.unlink(temp.name)


async def _forward_album(caption: str, events: list):
    temps = []
    try:
        for evt in events:
            if _has_uploadable_media(evt):
                temp = tempfile.NamedTemporaryFile(
                    delete=False, suffix=_media_suffix(evt)
                )
                temp.close()
                await evt.message.download_media(file=temp.name)
                temps.append(temp.name)

        if not temps:
            return

        if len(temps) == 1:
            await _send_to_target(caption, file_path=temps[0], event=events[0], queue=True)
        else:
            await _send_to_target(caption, album_paths=temps, event=events[0], queue=True)
    finally:
        for path in temps:
            try:
                os.unlink(path)
            except OSError:
                pass


async def _finish_album(gid: int):
    await asyncio.sleep(_ALBUM_TIMEOUT)
    events = _album_buffer.pop(gid, [])
    _album_tasks.pop(gid, None)
    raw_text = _album_caption.pop(gid, "")

    if not events:
        return

    _refresh_translator_model()

    caption = raw_text
    if raw_text:
        if await _try_handle_automatic_content(raw_text, events[0]):
            return
        if alerts.is_game_alert(raw_text):
            parsed = alerts.parse(raw_text)
            if parsed:
                caption = alerts.format_farsi(parsed) or raw_text
        else:
            try:
                first_evt = events[0]
                html = _message_to_html(raw_text, first_evt.message.entities)
                html = _strip_hashtags(html)
                html = _strip_quotes(html)
                links = _extract_urls(first_evt)
                link_url = await _display_link_url(links)
                for url in links:
                    html = html.replace(url, "")
                html = html.strip()
                if html:
                    translated = _fix_unclosed_tags(_strip_quotes(await translator.translate(html)))
                    caption = _build_caption(translated, link_url=link_url, html=True)
                else:
                    caption = _build_caption(None, link_url=link_url)
            except Exception as e:
                logger.error("Translation error for album: %s", e)
                caption = raw_text

    logger.info("Processing album %d: %d items", gid, len(events))

    await _forward_album(caption, events)
    await _send_notification(events[0], caption)


async def handle_new_message(event, *, automatic_only: bool = False):
    _refresh_translator_model()
    text = event.message.text
    media = event.message.media
    grouped_id = event.message.grouped_id

    if not text and not media:
        return

    # These source formats must stay out of the generic chunk/LLM pipeline:
    # price risers and fallers arrive as separate messages, while lineups and
    # live alerts need their database-backed Farsi formatting immediately.
    if text and await _try_handle_automatic_content(text, event):
        return

    if automatic_only:
        logger.info("Ignoring non-automatic post from automatic-only source")
        return

    # Merge text chunks split by Telegram's character limit
    if text and not event.message.media and not event.message.grouped_id:
        chat_id = event.chat_id
        if chat_id not in _chunk_buffer:
            _chunk_buffer[chat_id] = []
        _chunk_buffer[chat_id].append(event)

        if chat_id in _chunk_tasks:
            _chunk_tasks[chat_id].cancel()
        _chunk_tasks[chat_id] = asyncio.create_task(_finish_chunks(chat_id))
        return

    # Album messages: buffer and process together
    if grouped_id:
        if grouped_id not in _album_buffer:
            _album_buffer[grouped_id] = []
        _album_buffer[grouped_id].append(event)
        if text:
            _album_caption[grouped_id] = text

        if grouped_id not in _album_tasks:
            _album_tasks[grouped_id] = asyncio.create_task(
                _finish_album(grouped_id)
            )
        return

    html = _message_to_html(text or "", event.message.entities)
    html = _strip_hashtags(html)
    html = _strip_quotes(html)
    links = _extract_urls(event)
    link_url = await _display_link_url(links)
    for url in links:
        html = html.replace(url, "")
    html = html.strip()

    translated = None
    if html:
        try:
            if len(_strip_html_tags(html)) > _ARTICLE_SOURCE_THRESHOLD:
                source_html, allowed_links = _prepare_article_source(html)
                result = await translator.translate_article(source_html)
                title = _article_title(result.get("title", ""))
                summary = result.get("summary", "")
                body = articles.sanitize_article_links(
                    _fix_unclosed_tags(_strip_quotes(result.get("body", ""))),
                    allowed_links,
                )
                telegraph_url = articles.publish_to_telegraph(
                    title,
                    body,
                    summary=summary,
                    source_tag=_catalog_source_tag(event),
                )
                if telegraph_url:
                    caption = _format_telegraph_post(title, summary, telegraph_url)
                    await _send_to_target(caption, event=event, queue=True)
                    await _send_notification(event, caption)
                    logger.info("Published Telegraph article (%d chars)", len(body))
                else:
                    caption = _build_caption(body, link_url=link_url, html=True)
                    await _forward_message(caption, event)
                    await _send_notification(event, caption)
                    logger.info("Telegraph failed, posted inline")
            else:
                translated = _fix_unclosed_tags(_strip_quotes(await translator.translate(html)))
        except TranslationError as e:
            logger.error("Translation error: %s", e)
            return
        except Exception as e:
            logger.error("Unexpected translation error: %s", e)
            return

    if translated:
        caption = _build_caption(translated, link_url=link_url, html=True)
        await _forward_message(caption, event)
        logger.info("Forwarded message to %s", _target_channel())
        await _send_notification(event, caption)

    await _maybe_post_article(text or "", event)


async def _finish_chunks(chat_id: int):
    await asyncio.sleep(_CHUNK_TIMEOUT)
    chunks = _chunk_buffer.pop(chat_id, [])
    _chunk_tasks.pop(chat_id, None)
    if not chunks:
        return

    _refresh_translator_model()

    merged_text = "\n".join(evt.message.text for evt in chunks if evt.message.text)
    first_evt = chunks[0]
    logger.info("Merged %d text chunks from chat %d (%d chars)", len(chunks), chat_id, len(merged_text))

    # A source could still have split an exceptionally long automatic post.
    # Try the merged text before translating it as a generic article.
    if await _try_handle_automatic_content(merged_text, first_evt):
        return

    html = _message_to_html(merged_text, first_evt.message.entities)
    html = _strip_hashtags(html)
    html = _strip_quotes(html)
    links = _extract_urls(first_evt)
    link_url = await _display_link_url(links)
    for url in links:
        html = html.replace(url, "")
    html = html.strip()

    translated = None
    if html:
        try:
            source_html, allowed_links = _prepare_article_source(html)
            result = await translator.translate_article(source_html)
            title = _article_title(result.get("title", ""))
            summary = result.get("summary", "")
            body = articles.sanitize_article_links(
                _fix_unclosed_tags(_strip_quotes(result.get("body", ""))),
                allowed_links,
            )
            telegraph_url = articles.publish_to_telegraph(
                title,
                body,
                summary=summary,
                source_tag=_catalog_source_tag(first_evt),
            )
            if telegraph_url:
                caption = _format_telegraph_post(title, summary, telegraph_url)
                await _send_to_target(caption, event=first_evt, queue=True)
                await _send_notification(first_evt, caption)
            else:
                caption = _build_caption(body, link_url=link_url, html=True)
                await _forward_message(caption, first_evt)
                await _send_notification(first_evt, caption)
        except TranslationError as e:
            logger.error("Translation error for chunks: %s", e)
            return
        except Exception as e:
            logger.error("Unexpected translation error for chunks: %s", e)
            return

    await _maybe_post_article(merged_text, first_evt)


async def _maybe_post_article(text: str, event):
    urls = []
    for url in _extract_urls(event):
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)

    for url in urls:
        logger.info("Post-processing possible article URL: %s", url)
        try:
            posted = await _publish_article_from_url(url, event=event)
        except Exception as exc:
            logger.info("Article post-processing error for %s: %s", url, exc)
            continue
        if posted:
            return


async def _publish_article_from_url(url: str, *, event=None) -> bool:
    """Extract, translate, and schedule a readable article from a web URL."""
    article = await asyncio.to_thread(articles.fetch_article, url)
    if not article:
        return False

    feature_image = article.get("feature_image") or article.get("header_image", "")
    if not feature_image:
        feature_image = next(
            (
                part.get("src", "")
                for part in article.get("parts", [])
                if part.get("type") == "img" and part.get("src")
            ),
            "",
        )
    if not feature_image:
        feature_image = next(iter(article.get("images", [])), "")
    if not feature_image:
        raise RuntimeError("مقاله تصویر شاخص ندارد و تصویری برای انتشار پیدا نشد.")

    if article.get("parts"):
        raw_html = articles.build_article_html(
            article["title"], article["date"], article["summary"],
            article["parts"], article["url"],
        )
    else:
        raw_html = articles.build_general_article_html(article)

    raw_html = articles.remove_images_from_html(
        raw_html, [feature_image], article.get("url", url)
    )

    translation_html, inline_images = articles.prepare_article_html(raw_html)
    # The source's own cross-links have already been rerouted to our published
    # translations of those articles. Keep the list so any other hyperlink the
    # model returns can be dropped.
    allowed_links = articles.article_link_targets(translation_html)
    result = await translator.translate_article(translation_html)
    if not result.get("complete", True):
        raise ContentIncompleteError(
            str(result.get("incomplete_reason") or "article source looks truncated")
        )
    title = _article_title(
        _fix_unclosed_tags(_strip_quotes(result.get("title", ""))), article["title"]
    )
    summary = _fix_unclosed_tags(_strip_quotes(result.get("summary", "")))
    translated = _fix_unclosed_tags(_strip_quotes(result.get("body", "")))
    if not translated.strip():
        raise RuntimeError("Article translation was empty.")

    # Prefer the image positions represented in the extracted article HTML.
    # The article-level image list is only a fallback for pages whose reader
    # mode omitted all inline images (for example, lazy-loaded images).
    source_images = inline_images or article.get(
        "images", [article.get("header_image", "")]
    )
    source_images = [image for image in source_images if image != feature_image]
    kept_images = [
        image
        for index, image in enumerate(source_images, start=1)
        if index not in (result.get("removed_images") or set())
    ]

    # Telegraph is for what genuinely needs a page. A translation that fits in
    # a caption and carries no inline image reads better as an ordinary post
    # with its feature image, so the decision is made here, on the finished
    # Persian text, rather than guessed from the length of the English source.
    if not kept_images:
        # The Telegraph path sanitizes links while restoring images; a caption
        # never goes through that, so it is cleaned here.
        inline_body = articles.strip_image_markers(
            articles.sanitize_article_links(translated, allowed_links)
        )
        caption = _format_short_article_post(
            title,
            inline_body,
            article["url"],
            source_name=article.get("source_name", ""),
        )
        body_length = _rendered_text_length(inline_body)
        caption_length = _caption_length(caption)
        if caption_length <= _MEDIA_CAPTION_LIMIT:
            feature_path = await asyncio.to_thread(
                _download_remote_media, feature_image, "photo"
            )
            try:
                await _send_to_target(
                    caption, file_path=feature_path, event=event, queue=True
                )
                if event:
                    await _send_notification(event, caption, is_media=True)
                else:
                    await _send_notification(None, caption, source="Article", is_media=True)
            finally:
                Path(feature_path).unlink(missing_ok=True)
            logger.info(
                "Published %s as a featured-image post (%d body chars, %d caption units)",
                url,
                body_length,
                caption_length,
            )
            return True
        logger.info(
            "Article translation needs Telegraph: %d body chars, %d caption units",
            body_length,
            caption_length,
        )

    translated = articles.restore_images_in_place(
        translated,
        source_images,
        source_html=translation_html,
        removed_images=result.get("removed_images") or set(),
        allowed_links=allowed_links,
    )
    translated = articles.append_original_article_link(
        translated, article["url"], article.get("source_name", "")
    )

    feature_path = await asyncio.to_thread(
        _download_remote_media, feature_image, "photo"
    )
    try:
        telegraph_url = articles.publish_to_telegraph(
            title,
            translated,
            raise_on_error=True,
            summary=summary,
            source_tag=article.get("source_name", ""),
            image_url=feature_image,
            source_url=article.get("url", url),
        )
        if not telegraph_url:
            raise RuntimeError("Telegraph returned no article URL.")

        caption = _format_telegraph_post(
            title,
            summary,
            telegraph_url,
        )
        await _send_to_target(
            caption, file_path=feature_path, event=event, queue=True
        )
        if event:
            await _send_notification(event, caption, is_media=True)
        else:
            await _send_notification(None, caption, source="Article", is_media=True)
    finally:
        Path(feature_path).unlink(missing_ok=True)
    logger.info("Published article from %s", url)
    return True


async def _prepare_x_post(url: str) -> str:
    """Fetch, translate, and put an X post/thread in Telegram's scheduled queue."""
    if not settings.x_bearer_token and not settings.x_rapidapi_key:
        raise RuntimeError("Set X_RAPIDAPI_KEY or X_BEARER_TOKEN to import X posts.")
    posts = await asyncio.to_thread(
        x_posts.fetch_post_and_thread, url, settings.x_bearer_token, settings.x_rapidapi_key
    )
    _refresh_translator_model()
    prepared = []
    for post in posts:
        translated = ""
        if post.text:
            x_text = re.sub(r"(?<!\w)#\w+", "", post.text)
            x_text = re.sub(r"[ \t]{2,}", " ", x_text)
            x_text = re.sub(r" *\n *", "\n", x_text).strip()
            x_text = await asyncio.to_thread(_expand_x_short_links, x_text)
            translated_source = _x_text_to_html(x_text)
            translated = _fix_unclosed_tags(
                _strip_quotes(await translator.translate(translated_source))
            )
        source_url = f"https://x.com/{post.author}/status/{post.id}"
        caption = _build_caption(
            translated, link_url=source_url, html=True, link_label="لینک منبع"
        )
        prepared.append((post, caption))
    await _schedule_x_posts(prepared)
    return f"✅ {len(prepared)} پست X برای بررسی، {QUEUED_POST_CONFIRMATION}"


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


def _download_remote_media(url: str, kind: str) -> str:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    suffix = Path(urlparse(url).path).suffix
    if not suffix:
        suffix = ".mp4" if kind in {"video", "animated_gif"} else ".jpg"
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        temp.write(response.content)
    finally:
        temp.close()
    return temp.name


def _download_x_media(media: x_posts.Media) -> str:
    return _download_remote_media(media.url, media.kind)


async def _import_youtube_transcript(url: str) -> str:
    """Schedule a Persian YouTube post, inline for short transcripts."""
    metadata = await asyncio.to_thread(
        youtube_posts.fetch_video_metadata, url, settings.youtube_api_key
    )
    transcript = await asyncio.to_thread(
        youtube_posts.fetch_english_transcript, url, settings.x_rapidapi_key
    )
    # Providers sometimes return only the opening minutes of a video. Check the
    # captured transcript against the video's real duration before spending a
    # translation on it, so a partial transcript is retried rather than posted.
    assessment = await translator.assess_completeness(
        transcript,
        kind="YouTube video transcript",
        duration_seconds=metadata.duration_seconds,
    )
    if not assessment.get("complete", True):
        raise ContentIncompleteError(
            assessment.get("reason") or "transcript looks incomplete"
        )
    # Speech recognition mangles player names before anything is translated, so
    # this pass is given the whole roster; every other path resolves the names
    # it actually needs from the same index.
    player_glossary = await asyncio.to_thread(player_names.glossary)
    transcript = await translator.correct_transcript(transcript, player_glossary)
    _refresh_translator_model()
    # YouTube titles carry search tags and self-promotion that mean nothing in
    # the channel. The model is asked to drop them while translating; the
    # scripted pass afterwards is a net for the mechanical leftovers.
    translated_title = youtube_posts.clean_video_title(
        _fix_unclosed_tags(
            _strip_quotes(
                await translator.translate(
                    _escape_html(metadata.title),
                    instructions=VIDEO_TITLE_INSTRUCTIONS,
                )
            )
        )
    )
    article = await translator.translate_article(
        _escape_html(transcript), transcript=True,
    )
    # A transcript contains no hyperlinks, so any link in the translation was
    # invented by the model.
    article_body = articles.sanitize_article_links(
        _fix_unclosed_tags(_strip_quotes(article.get("body", "")))
    )
    if not article_body.strip():
        raise RuntimeError("ترجمهٔ زیرنویس خالی بود.")

    # Short videos read better as an ordinary post with the thumbnail than as a
    # Telegraph page. The English transcript's length predicts neither the
    # Persian length nor the caption's, so the finished text decides.
    inline_caption = _format_youtube_inline_post(
        translated_title,
        _telegraph_to_telegram_html(article_body),
        url,
    )
    transcript_length = _rendered_text_length(article_body)
    caption_length = _caption_length(inline_caption)
    if caption_length <= _MEDIA_CAPTION_LIMIT:
        thumbnail = await asyncio.to_thread(
            _download_remote_media, metadata.thumbnail_url, "photo"
        )
        try:
            await _send_to_target(
                inline_caption, file_path=thumbnail, queue=True,
            )
            await _send_notification(
                None, inline_caption, source="YouTube", is_media=True,
            )
        finally:
            Path(thumbnail).unlink(missing_ok=True)
        logger.info(
            "Published %s as an inline post (%d body chars, %d caption units)",
            url,
            transcript_length,
            caption_length,
        )
        return (
            f"✅ متن فارسی کوتاه برای بررسی، {QUEUED_POST_CONFIRMATION}"
        )
    logger.info(
        "Transcript needs Telegraph: %d body chars, %d caption units",
        transcript_length,
        caption_length,
    )

    article_title = _article_title(
        translated_title or _fix_unclosed_tags(_strip_quotes(article.get("title", "")))
    )
    translated_description = ""
    description = youtube_posts.description_before_first_link_sentence(metadata.description)
    if description:
        translated_description = _fix_unclosed_tags(
            _strip_quotes(await translator.translate(_escape_html(description)))
        )
    article_summary = _fix_unclosed_tags(_strip_quotes(article.get("summary", "")))
    if not article_summary:
        article_summary = _youtube_description_preview(translated_description)
    article_body += (
        f'\n\n<p><a href="{_escape_html(url)}">مشاهدهٔ ویدیوی اصلی در YouTube</a></p>'
    )
    telegraph_url = articles.publish_to_telegraph(
        article_title,
        article_body,
        summary=article_summary,
        source_tag=metadata.channel_title,
        image_url=metadata.thumbnail_url,
        source_url=url,
    )
    if not telegraph_url:
        raise RuntimeError("ساخت مقاله در Telegraph ناموفق بود.")
    article_caption = _format_youtube_telegraph_post(
        article_title,
        article_summary,
        telegraph_url,
        url,
    )
    thumbnail = await asyncio.to_thread(
        _download_remote_media, metadata.thumbnail_url, "photo"
    )
    try:
        await _send_to_target(
            article_caption, file_path=thumbnail, queue=True,
        )
        await _send_notification(
            None, article_caption, source="YouTube", is_media=True,
        )
    finally:
        Path(thumbnail).unlink(missing_ok=True)
    return (
        f"✅ مقالهٔ فارسی برای بررسی، {QUEUED_POST_CONFIRMATION}"
    )


def _is_forwarded_message(event) -> bool:
    """Return whether Telegram marked this private admin message as forwarded."""
    message = getattr(event, "message", None)
    return bool(
        getattr(message, "fwd_from", None)
        or getattr(message, "forward", None)
    )


def _forwarded_post_html(event) -> tuple[str, list[str]]:
    """Extract one forwarded post's text while preserving its formatting."""
    message = event.message
    text = message.text or ""
    html = _strip_quotes(_strip_hashtags(_message_to_html(text, message.entities)))
    links = _extract_urls(event)
    for url in links:
        html = html.replace(url, "")
    html = _prepare_plain_article_layout(html).strip()
    if html and not re.search(r"<(?:p|h[1-6]|blockquote|ul|ol|pre)\b", html, re.IGNORECASE):
        html = f"<p>{html}</p>"
    return html, links


@dataclass
class _ForwardedPost:
    """One post of a forwarded burst, which may be an album of several messages."""

    html: str
    links: list[str]
    photo_event: object | None


def _is_photo_message(event) -> bool:
    """Whether this message carries an image that could open the article.

    A forwarded video or file is still a member of the batch and its caption is
    still used, but it is not something to publish as a feature image.
    """
    message = event.message
    if message.photo:
        return True
    mime = str(getattr(getattr(message, "document", None), "mime_type", "") or "")
    return mime.startswith("image/")


def _forwarded_batch_posts(events: list) -> list[_ForwardedPost]:
    """Turn the raw burst into posts, collapsing each album into one.

    Telegram delivers an album as one message per item, with the caption on
    whichever item happens to carry it, so counting them individually would
    treat one post as several and lose the caption's place in the sequence.
    """
    posts: list[_ForwardedPost] = []
    by_album: dict[int, _ForwardedPost] = {}
    for event in events:
        html, links = _forwarded_post_html(event)
        photo_event = event if _is_photo_message(event) else None
        album = getattr(event.message, "grouped_id", None)
        post = by_album.get(album) if album is not None else None
        if post is None:
            post = _ForwardedPost(html=html, links=list(links), photo_event=photo_event)
            posts.append(post)
            if album is not None:
                by_album[album] = post
            continue
        if html and not post.html:
            post.html = html
        post.links.extend(link for link in links if link not in post.links)
        if post.photo_event is None:
            post.photo_event = photo_event
    return posts


async def _download_forwarded_photo(event) -> str | None:
    """Download one forwarded image to a temporary file, or return None."""
    if event is None:
        return None
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=_media_suffix(event))
    temp.close()
    try:
        await event.message.download_media(file=temp.name)
    except Exception:
        logger.exception("Could not download the forwarded article's feature image")
        Path(temp.name).unlink(missing_ok=True)
        return None
    if not os.path.getsize(temp.name):
        Path(temp.name).unlink(missing_ok=True)
        return None
    return temp.name


async def _translate_forwarded_article(events: list) -> str:
    """Merge a burst of forwarded posts into one Telegraph article."""
    first_event = events[0]
    posts = _forwarded_batch_posts(events)
    parts = [post.html for post in posts if post.html]
    all_links: list[str] = []
    for post in posts:
        all_links.extend(link for link in post.links if link not in all_links)
    # The first image opens the article; the rest are dropped, but every post's
    # text is part of the chain whether or not it came with a picture.
    feature_event = next(
        (post.photo_event for post in posts if post.photo_event is not None), None
    )
    logger.info(
        "Merging %d forwarded post(s) into one article: %d with text, %d with an image",
        len(posts),
        len(parts),
        sum(1 for post in posts if post.photo_event is not None),
    )
    merged_html = "\n\n".join(parts).strip()
    if not merged_html:
        raise ValueError("پست‌های فورواردشده متن قابل استفاده‌ای ندارند.")

    merged_html, allowed_links = articles.reroute_html_links(merged_html)
    result = await translator.translate_article(merged_html, merged_posts=len(parts) > 1)
    title = _article_title(_fix_unclosed_tags(_strip_quotes(result.get("title", ""))))
    summary = _fix_unclosed_tags(_strip_quotes(result.get("summary", "")))
    body = articles.sanitize_article_links(
        _fix_unclosed_tags(_strip_quotes(result.get("body", ""))), allowed_links
    )
    feature_path = await _download_forwarded_photo(feature_event)
    attachment = feature_path
    try:
        telegraph_url = articles.publish_to_telegraph(
            title,
            body,
            summary=summary,
            source_tag="Telegram",
        )
        if telegraph_url:
            caption = _format_telegraph_post(title, summary, telegraph_url)
            if attachment and _rendered_text_length(caption) > _MEDIA_CAPTION_LIMIT:
                # Telegram refuses an over-long caption on a media post, and the
                # article is worth more than the picture that opens it.
                logger.info(
                    "Merged-article caption is %d characters; posting it without the feature image",
                    _rendered_text_length(caption),
                )
                attachment = None
            await _send_to_target(
                caption, file_path=attachment, event=first_event, queue=True
            )
        else:
            attachment = None
            link_url = await _display_link_url(all_links)
            caption = _build_caption(body, link_url=link_url, html=True)
            await _forward_message(caption, first_event)
    finally:
        if feature_path:
            Path(feature_path).unlink(missing_ok=True)
    await _send_notification(first_event, caption, is_media=bool(attachment))
    return (
        f"✅ {len(posts)} پست فورواردشده به یک مقالهٔ واحد تبدیل شد"
        + ("، با تصویر شاخص" if attachment else "")
        + f"؛ {QUEUED_POST_CONFIRMATION}"
    )


async def _finish_forwarded_batch(chat_id: int) -> None:
    loop = asyncio.get_running_loop()
    # Wait out the quiet window rather than restarting a task per message: a
    # later post extends the deadline, and once the wait ends the batch is
    # closed and handed on. Cancelling the task instead, as this used to, could
    # abort a publish that had already started and leave the admin's dashboard
    # call waiting for a reply that never came.
    while True:
        remaining = _forward_batch_deadlines.get(chat_id, 0.0) - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(remaining)

    events = _forward_batch_buffer.pop(chat_id, [])
    waiters = _forward_batch_waiters.pop(chat_id, [])
    _forward_batch_deadlines.pop(chat_id, None)
    # Released before publishing, so a post arriving during the publish opens a
    # new batch instead of joining one that has already been taken.
    _forward_batch_tasks.pop(chat_id, None)

    result = ""
    try:
        if len(events) == 1:
            result = await _translate_dashboard_single_submission(events[0])
        elif events:
            result = await _translate_forwarded_article(events)
    except Exception as exc:
        logger.exception("Forwarded batch of %d post(s) failed", len(events))
        result = f"❌ {exc}"
    finally:
        for index, waiter in enumerate(waiters):
            if waiter.done():
                continue
            # One confirmation is enough for the whole burst; the other callback
            # invocations suppress their duplicate dashboard replies.
            waiter.set_result(result if index == 0 else "")


async def _queue_forwarded_submission(event) -> str:
    chat_id = event.chat_id
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    _forward_batch_buffer.setdefault(chat_id, []).append(event)
    _forward_batch_waiters.setdefault(chat_id, []).append(waiter)
    window = (
        _FORWARDED_BATCH_MEDIA_TIMEOUT
        if any(
            _has_uploadable_media(queued)
            for queued in _forward_batch_buffer[chat_id]
        )
        else _FORWARDED_BATCH_TIMEOUT
    )
    _forward_batch_deadlines[chat_id] = loop.time() + window
    if chat_id not in _forward_batch_tasks:
        _forward_batch_tasks[chat_id] = asyncio.create_task(
            _finish_forwarded_batch(chat_id)
        )
    return await waiter


async def _translate_dashboard_submission(event) -> str:
    """Translate an admin submission, batching forwarded posts into articles."""
    if _is_forwarded_message(event):
        return await _queue_forwarded_submission(event)
    return await _translate_dashboard_single_submission(event)


async def _translate_dashboard_single_submission(event) -> str:
    """Translate one admin's private bot submission through the normal pipeline."""
    _refresh_translator_model()
    text = event.message.text or ""
    if text and await _try_handle_automatic_content(text, event):
        return "✅ هشدار خودکار بدون ترجمه ارسال شد."

    html = _strip_quotes(_strip_hashtags(_message_to_html(text, event.message.entities)))
    links = _extract_urls(event)
    link_url = await _display_link_url(links)
    for url in links:
        html = html.replace(url, "")
    html = html.strip()

    if html and len(_strip_html_tags(html)) > _ARTICLE_SOURCE_THRESHOLD:
        source_html, allowed_links = _prepare_article_source(html)
        result = await translator.translate_article(source_html)
        title = _article_title(_fix_unclosed_tags(_strip_quotes(result.get("title", ""))))
        summary = _fix_unclosed_tags(_strip_quotes(result.get("summary", "")))
        body = articles.sanitize_article_links(
            _fix_unclosed_tags(_strip_quotes(result.get("body", ""))), allowed_links
        )
        telegraph_url = articles.publish_to_telegraph(
            title, body, summary=summary, source_tag="Telegram",
        )
        if telegraph_url:
            caption = _format_telegraph_post(title, summary, telegraph_url)
            await _send_to_target(caption, event=event, queue=True)
        else:
            caption = _build_caption(body, link_url=link_url, html=True)
            await _forward_message(caption, event)
    elif html:
        translated = _fix_unclosed_tags(_strip_quotes(await translator.translate(html)))
        caption = _build_caption(translated, link_url=link_url, html=True)
        await _forward_message(caption, event)
    elif event.message.media:
        caption = AI_SIGNATURE
        await _forward_message(caption, event)
    else:
        raise ValueError("متن یا رسانه‌ای برای ترجمه پیدا نشد.")

    await _send_notification(event, caption)
    return (
        f"✅ ترجمه برای بررسی، {QUEUED_POST_CONFIRMATION}"
    )


async def _schedule_x_posts(prepared: list[tuple[x_posts.Post, str]]) -> None:
    for post, caption in prepared:
        paths = []
        try:
            paths = [await asyncio.to_thread(_download_x_media, media) for media in post.media]
            if len(paths) > 1:
                await _send_to_target(caption, album_paths=paths, queue=True)
            elif paths:
                await _send_to_target(caption, file_path=paths[0], queue=True)
            else:
                await _send_to_target(caption, queue=True)
            await _send_notification(
                None, caption, source=f"X: @{post.author}", is_media=bool(post.media)
            )
        finally:
            for path in paths:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _fetch_openrouter_balance() -> dict:
    response = requests.get(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        timeout=25,
    )
    response.raise_for_status()
    return response.json().get("data", response.json())


def _fetch_google_aistudio_status() -> dict:
    if not settings.google_aistudio_key:
        return {"configured": False}
    try:
        response = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": settings.google_aistudio_key},
            params={"pageSize": 100},
            timeout=25,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {"configured": True, "available": False, "error": str(exc)}
    if not response.ok:
        return {
            "configured": True,
            "available": False,
            "error": data.get("error", {}).get("message", f"HTTP {response.status_code}"),
        }
    models = {
        model.get("name", "").removeprefix("models/")
        for model in data.get("models", [])
        if "generateContent" in model.get("supportedGenerationMethods", [])
    }
    return {
        "configured": True,
        "available": True,
        "model_available": settings.google_aistudio_model in models,
    }


async def _openrouter_balance() -> str:
    data, google = await asyncio.gather(
        asyncio.to_thread(_fetch_openrouter_balance),
        asyncio.to_thread(_fetch_google_aistudio_status),
    )
    limit = data.get("limit")
    usage = data.get("usage")
    remaining = data.get("limit_remaining")
    def display(value):
        return "نامحدود" if value is None else str(value)
    tier = "رایگان" if data.get("is_free_tier") else "اعتباری"
    google_status = "تنظیم نشده"
    if google.get("configured"):
        if google.get("available"):
            connection = "متصل"
            model_status = "فعال" if google.get("model_available") else "مدل پیدا نشد"
            google_status = (
                f"اتصال: <b>{connection}</b>\n"
                f"مدل: <code>{settings.google_aistudio_model}</code> ({model_status})\n"
                "مصرف/سهمیه: در API عمومی قابل دریافت نیست؛ در AI Studio قابل مشاهده است"
            )
        else:
            google_status = f"خطا: <b>{_escape_html(google.get('error', 'نامشخص'))}</b>"
    return (
        "<b>💳 اعتبار OpenRouter</b>\n\n"
        f"نوع حساب: <b>{tier}</b>\n"
        f"سقف: <b>{display(limit)}</b>\n"
        f"مصرف: <b>{display(usage)}</b>\n"
        f"مانده: <b>{display(remaining)}</b>\n\n"
        "<b>🔹 Google AI Studio</b>\n\n"
        f"{google_status}\n\n"
        '<a href="https://aistudio.google.com/">مشاهدهٔ سهمیه در Google AI Studio</a>'
    )


async def _telegraph_editor_url(article_url: str) -> str:
    return telegraph_editor.create_edit_url(article_url)


async def _recent_telegraph_pages() -> list[dict]:
    import article_catalog

    await asyncio.to_thread(article_catalog.sync_from_telegraph)
    # The private editor must also show queued (not-yet-public) articles.
    return await asyncio.to_thread(
        article_catalog.list_pages, limit=50, include_hidden=True
    )


async def _enrich_article_catalog() -> None:
    """Backfill source links, images, and AI summaries for indexed pages.

    This runs once per start and works through the whole backlog in batches
    rather than only the first page of it. An indexed article that never
    recovers its source URL can never be linked to by a later translation, so
    a catalog built up before internal-link rerouting existed would otherwise
    stay unusable for it indefinitely.
    """
    import article_catalog

    if not os.getenv("TELEGRAPH_ACCESS_TOKEN"):
        return
    attempted: set[str] = set()
    try:
        await asyncio.to_thread(article_catalog.sync_from_telegraph)
        while len(attempted) < _CATALOG_ENRICHMENT_LIMIT:
            batch = await asyncio.to_thread(
                article_catalog.pages_needing_enrichment,
                _CATALOG_ENRICHMENT_BATCH,
            )
            pages = [page for page in batch if page["path"] not in attempted]
            if not pages:
                # Every page still needing work has already had its turn.
                break
            for page in pages:
                attempted.add(page["path"])
                await _enrich_catalog_page(page)
                # A whole-backlog sweep asks Telegraph, and sometimes a source
                # site, for one page after another. Nothing waits on this, so
                # pace it rather than risk a rate limit.
                await asyncio.sleep(_CATALOG_ENRICHMENT_DELAY)
        if attempted:
            logger.info("Enriched %d Telegraph catalog page(s)", len(attempted))
    except Exception:
        logger.exception("Telegraph catalog enrichment failed")


async def _enrich_catalog_page(page: dict) -> None:
    """Recover one indexed page's source link, image, and AI summary."""
    import article_catalog

    try:
        remote = await asyncio.to_thread(
            articles.get_telegraph_page, page["url"]
        )
        content = str(remote.get("content") or "")
        source_url = page.get("source_url") or article_catalog.first_source_url(content)
        image_url = (
            page.get("image_url")
            or article_catalog.first_image_url(content)
        )
        if not image_url and source_url:
            image_url = _youtube_thumbnail_url(source_url)
            if not image_url:
                source_article = await asyncio.to_thread(
                    articles.fetch_article, source_url
                )
                if source_article:
                    image_url = (
                        source_article.get("feature_image")
                        or source_article.get("header_image", "")
                        or next(iter(source_article.get("images", [])), "")
                    )
        summary = page.get("summary", "")
        generated_summary = ""
        if page.get("summary_source") != "ai" or not summary:
            generated_summary = await translator.summarize_article(content)
            summary = generated_summary or summary
        await asyncio.to_thread(
            article_catalog.update_metadata,
            page["url"],
            summary=summary,
            image_url=image_url,
            source_url=source_url,
            summary_source="ai" if generated_summary else page.get(
                "summary_source", "telegraph"
            ),
        )
    except Exception:
        logger.exception(
            "Failed to enrich Telegraph catalog page %s", page.get("url")
        )
    finally:
        # Pages are taken never-attempted first, then oldest attempt first, so
        # every page must be stamped whether or not anything was recovered.
        await asyncio.to_thread(
            article_catalog.mark_enrichment_attempt, page.get("url", "")
        )


async def _article_catalog_url() -> str:
    return telegraph_editor.public_catalog_url()


async def _import_article(url: str) -> str:
    """Publish a general web article requested from the private dashboard."""
    _refresh_translator_model()
    if not await _publish_article_from_url(url):
        raise RuntimeError(
            "نسخهٔ خواندنی این صفحه پیدا نشد یا صفحه نیاز به ورود/اشتراک دارد."
        )
    return (
        f"✅ مقالهٔ فارسی برای بررسی، {QUEUED_POST_CONFIRMATION}"
    )


def _gameweek_has_football_left(gameweek_id: int) -> bool:
    """Whether this gameweek still has a match to come or in progress.

    A fixture that has been unfinished for longer than a match can last was
    postponed rather than played, and must not hold a gameweek open until the
    rescheduled date.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=_FIXTURE_IN_PROGRESS_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bool(
        db.query_scalar(
            "SELECT count(*) FROM fixtures "
            "WHERE gameweek_id=? AND finished=0 AND kickoff_time > ?",
            (gameweek_id, cutoff),
        )
    )


def _fixtures_gameweek() -> dict:
    """Return the gameweek whose fixtures are worth posting.

    The FPL API keeps a gameweek ``is_current`` until the *next* one's
    deadline, so between the final whistle and that deadline this command was
    still advertising matches that had already been played. Once the current
    gameweek has no football left in it, the next one is what people want.
    """
    current = db.query_one(
        "SELECT id, name FROM gameweeks WHERE is_current=1 ORDER BY id LIMIT 1"
    )
    if current:
        if _gameweek_has_football_left(current["id"]):
            return current
        upcoming = db.query_one(
            "SELECT g.id, g.name FROM gameweeks g "
            "JOIN fixtures f ON f.gameweek_id = g.id "
            "WHERE g.id > ? GROUP BY g.id ORDER BY g.id LIMIT 1",
            (current["id"],),
        )
        # Nothing follows the final gameweek of a season.
        return upcoming or current

    upcoming = db.query_one(
        "SELECT id, name FROM gameweeks WHERE is_next=1 ORDER BY id LIMIT 1"
    )
    if not upcoming:
        raise RuntimeError("گیم‌ویک فعلی یا بعدی پیدا نشد.")
    return upcoming


def _fixtures_text() -> str:
    gameweek = _fixtures_gameweek()
    fixtures = db.query(
        """SELECT f.kickoff_time, ht.short_name_fa AS home_fa, ht.short_name AS home_en,
                  at.short_name_fa AS away_fa, at.short_name AS away_en
           FROM fixtures f
           JOIN teams ht ON ht.id=f.team_h JOIN teams at ON at.id=f.team_a
           WHERE f.gameweek_id=? ORDER BY f.kickoff_time""",
        (gameweek["id"],),
    )
    if not fixtures:
        raise RuntimeError("بازی‌ای برای این گیم‌ویک پیدا نشد.")
    iran_offset = timedelta(hours=3, minutes=30)
    lines = [f"<b>📅 برنامهٔ {gameweek['name']}</b>", ""]
    current_day = None
    for fixture in fixtures:
        kickoff = datetime.strptime(fixture["kickoff_time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) + iran_offset
        # The post goes out weekly, so the weekday tells a reader which match
        # is which far quicker than a calendar date does.
        day = price_changes.persian_weekday(kickoff)
        if day != current_day:
            if current_day is not None:
                lines.append("")
            current_day = day
            lines.extend([f"<b>{day}</b>", ""])
        home = fixture["home_fa"] or fixture["home_en"]
        away = fixture["away_fa"] or fixture["away_en"]
        lines.append(f"<blockquote>{home} - {away} | <b>{kickoff.strftime('%H:%M')}</b></blockquote>")
    lines.extend(["", "@EPL_Fantasy"])
    return "\n".join(lines)


async def _prepare_dashboard_content(kind: str) -> str:
    global _pending_dashboard_content
    _pending_dashboard_content = None
    if kind == "fixtures":
        text = _fixtures_text()
    elif kind == "points":
        livefpl._games_cache = None
        fixtures = livefpl.get_finished_fixtures()
        if not fixtures:
            raise RuntimeError("هنوز بازی تمام‌شده‌ای برای امتیازات وجود ندارد.")
        fixture = max(fixtures, key=lambda item: item.get("kickoff_time", ""))
        text = await asyncio.to_thread(livefpl.build_game_text, fixture)
        if not text:
            raise RuntimeError("دادهٔ امتیازات این بازی از LiveFPL در دسترس نیست.")
    elif kind == "eo":
        livefpl._games_cache = None
        text = await asyncio.to_thread(livefpl.build_eo_text)
        if not text:
            raise RuntimeError("دادهٔ EO از LiveFPL در دسترس نیست.")
    elif kind == "prices":
        text = await asyncio.to_thread(livefpl.build_price_changes_text)
        if not text:
            raise RuntimeError("پیش‌بینی قیمت LiveFPL در دسترس نیست.")
    elif kind == "lineups":
        return (
            "<b>📋 ترکیب‌ها</b>\n\n"
            "ترکیب‌ها ۷۵ دقیقه پیش از شروع هر بازی از منبع رسمی لیگ برتر بررسی، "
            "با نام و قیمت فارسی فرمت و به‌صورت خودکار منتشر می‌شوند. "
            "برای این بخش انتشار دستی لازم نیست."
        )
    else:
        raise RuntimeError("نوع محتوا پشتیبانی نمی‌شود.")
    _pending_dashboard_content = text
    return f"<b>پیش‌نمایش</b>\n\n{text}"


async def _publish_dashboard_content() -> str:
    global _pending_dashboard_content
    if not _pending_dashboard_content:
        raise RuntimeError("پیش‌نمایش قابل انتشار وجود ندارد.")
    text = _pending_dashboard_content
    _pending_dashboard_content = None
    await _send_to_target(text)
    return "✅ محتوا در کانال منتشر شد."


async def _start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = await asyncio.start_server(telegraph_editor.handle_http, "0.0.0.0", port)
    logger.info("Health server listening on port %s", port)
    async with server:
        await server.serve_forever()


async def main():
    global _admin_bot_client
    logger.info("Starting TeleAdmin bot...")
    logger.info("  Target : %s", _target_channel())
    logger.info("  Model  : %s", runtime_config.get("OPEN_ROUTER_MODEL"))

    await client.start()
    await _ensure_automatic_sources()
    client.add_event_handler(_handle_configured_source_message, events.NewMessage(incoming=True))
    # Scheduled posts are emitted as normal channel messages when Telegram
    # publishes them. Edits are also observed in case a Telegraph link is
    # added or corrected after posting.
    client.add_event_handler(_handle_target_channel_message, events.NewMessage())
    client.add_event_handler(_handle_target_channel_message, events.MessageEdited())
    await _sync_catalog_visibility_from_target()
    logger.info("Bot is running. Press Ctrl+C to stop.")

    admin_client = None
    if settings.telegram_bot_token and settings.admin_user_ids:
        admin_client = TelegramClient(
            StringSession(), settings.telegram_api_id, settings.telegram_api_hash
        )
        await admin_client.start(bot_token=settings.telegram_bot_token)
        AdminDashboard(
            admin_client,
            settings.admin_user_ids,
            _prepare_x_post,
            _import_youtube_transcript,
            _openrouter_balance,
            _prepare_dashboard_content,
            _publish_dashboard_content,
            lambda value, actor_id: asyncio.to_thread(
                youtube_monitor.subscribe, value, settings.youtube_api_key, actor_id
            ),
            lambda value, actor_id: asyncio.to_thread(
                youtube_monitor.unsubscribe, value, settings.youtube_api_key, actor_id
            ),
            youtube_monitor.list_channels,
            _add_telegram_source,
            _remove_telegram_source,
            _telegram_source_list,
            _translate_dashboard_submission,
            _telegraph_editor_url,
            _recent_telegraph_pages,
            _import_article,
            _article_catalog_url,
        )
        _admin_bot_client = admin_client
        logger.info("Admin dashboard enabled for %d user(s)", len(settings.admin_user_ids))
    else:
        logger.warning("Admin dashboard disabled: set TELEGRAM_BOT_TOKEN and ADMIN_USER_IDS")

    # Build the player-name index before the first message arrives, so no
    # translation pays for it.
    await asyncio.to_thread(player_names.reload)

    tasks = [
        _start_health_server(),
        _enrich_article_catalog(),
        article_monitor.run_monitor(
            _publish_article_from_url, _notify_article_abandoned
        ),
        client.run_until_disconnected(),
        deadlines.run_deadline_loop(
            client=client,
            target_channel=_target_channel(),
            league_code=runtime_config.get("EPL_LEAGUE_CODE"),
        ),
        scheduler.run_scheduler(
            client=client,
            target_channel=_target_channel(),
            league_code=runtime_config.get("EPL_LEAGUE_CODE"),
            price_predictions_enabled=runtime_config.get_bool("PRICE_PREDICTIONS_ENABLED"),
        ),
        youtube_monitor.run_monitor(
            settings.youtube_api_key,
            _import_youtube_transcript,
            _notify_youtube_monitor_failure,
        ),
    ]
    if admin_client:
        tasks.append(admin_client.run_until_disconnected())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    client.loop.run_until_complete(main())
