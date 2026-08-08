import asyncio
import html as html_lib
import logging
import os
import re
import shutil
import sys
import tempfile
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
)

from config import load_config
from translator import Translator, TranslationError
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
from admin_dashboard import AdminDashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("TeleAdmin")

settings = load_config()
runtime_config.init()

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
_CHUNK_TIMEOUT = 3  # seconds to wait for text chunks from same chat
_YOUTUBE_DESCRIPTION_PREVIEW_LIMIT = 500
# Photo captions are limited to 1,024 rendered characters.  This leaves room
# for the YouTube header, title, original-video link, and AI signature.
_YOUTUBE_INLINE_TRANSCRIPT_LIMIT = 800

_album_buffer: dict[int, list] = {}
_album_tasks: dict[int, asyncio.Task] = {}
_album_caption: dict[int, str] = {}

_chunk_buffer: dict[int, list] = {}
_chunk_tasks: dict[int, asyncio.Task] = {}
_pending_dashboard_content: str | None = None
_admin_bot_client: TelegramClient | None = None
_schedule_slot_lock = asyncio.Lock()
# Telegram can briefly omit a message immediately after it is scheduled. Keep
# such reservations locally for a short grace period, but let the live
# scheduled-message history become authoritative afterward. This means a
# manually deleted future post frees its half-hour slot again.
_QUEUE_RESERVATION_GRACE_SECONDS = 45
_queue_reservations: dict[str, dict[tuple[int, int, int, int, int], tuple[datetime, float]]] = {}
_youtube_failure_notified: set[str] = set()


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
        return "هیچ کانال تلگرامی به‌عنوان منبع ثبت نشده است."
    rows = [
        f"<blockquote><b>{_escape_html(source['title'])}</b>\n<code>{_escape_html(source['source_ref'])}</code></blockquote>"
        for source in sources
    ]
    return "<b>📡 کانال‌های منبع تلگرام</b>\n\n" + "\n".join(rows)


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
    if not isinstance(entity, Channel) or not entity.broadcast:
        raise ValueError("فقط کانال‌های تلگرامی می‌توانند منبع باشند.")
    title = (entity.title or entity.username or str(entity.id)).strip()
    source_ref = f"@{entity.username}" if entity.username else str(entity.id)
    return entity.id, title, source_ref


async def _add_telegram_source(value: str, actor_id: int) -> str:
    channel_id, title, source_ref = await _resolve_telegram_source(value)
    if not runtime_config.add_telegram_source(channel_id, title, source_ref, actor_id):
        return f"<b>{_escape_html(title)}</b> از قبل در فهرست منابع است."
    return f"✅ کانال <b>{_escape_html(title)}</b> به فهرست منابع اضافه شد."


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
    return f"✅ کانال <b>{_escape_html(removed)}</b> از فهرست منابع حذف شد."


async def _handle_configured_source_message(event) -> None:
    channel_id = getattr(getattr(event.message, "peer_id", None), "channel_id", None)
    if channel_id is None or not runtime_config.is_telegram_source(channel_id):
        return
    await handle_new_message(event)


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
    title: str, summary: str, telegraph_url: str, *, source_name: str = "",
) -> str:
    summary_text = html_lib.unescape(_strip_html_tags(summary)).strip()
    source_suffix = f" {_escape_html(source_name)}" if source_name else ""
    post = (
        f"<b>✍ مقاله جدید{source_suffix}</b>\n\n"
        f"<b>{_escape_html(title)}</b>\n\n"
        f"- - - - - - - - -\n\n"
        f"{_escape_html(summary_text)}"
    )
    post += (
        f'\n\n<b><a href="{_escape_html(telegraph_url)}">'
        f"👈👈متن کامل فارسی مقاله👉👉</a></b>"
    )
    return f"{post}\n\n{AI_SIGNATURE}"


def _format_youtube_telegraph_post(
    title: str, summary: str, telegraph_url: str, original_url: str, channel_title: str,
) -> str:
    return (
        f"<b>▶️ ویدئوی جدید کانال {_escape_html(channel_title)}</b>\n\n"
        f"<b>{_escape_html(title)}</b>\n\n"
        f"- - - - - - - - -\n\n"
        f"{summary}\n\n"
        f'<a href="{_escape_html(original_url)}">مشاهدهٔ ویدیوی اصلی در YouTube</a>\n\n'
        f'<b><a href="{_escape_html(telegraph_url)}">👈👈متن کامل فارسی ویدئو👉👉</a></b>\n\n'
        f"{AI_SIGNATURE}"
    )


def _format_youtube_inline_post(
    title: str, transcript: str, original_url: str, channel_title: str,
) -> str:
    return (
        f"<b>▶️ ویدئوی جدید کانال {_escape_html(channel_title)}</b>\n\n"
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
    urls = []
    if event.message.text:
        urls.extend(m.group(0) for m in _URL_RE.finditer(event.message.text))
    if event.message.entities:
        for entity in event.message.entities:
            url = getattr(entity, "url", None)
            if url:
                urls.append(url)
    return urls


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
    """Alert admins once when every transcript provider failed for a video."""
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


async def _next_queue_slot(target_channel: str) -> datetime:
    scheduled = await client.get_messages(target_channel, limit=100, scheduled=True)
    occupied = [message.date for message in scheduled if getattr(message, "date", None)]
    target_key = str(target_channel)
    now = datetime.now(tz=timezone.utc)
    scheduled_keys = {
        post_queue._slot_key(value) for value in occupied if value.tzinfo is not None
    }
    reservations = _queue_reservations.setdefault(target_key, {})
    for slot_key, (reserved_slot, reserved_at) in list(reservations.items()):
        if (
            slot_key in scheduled_keys
            or now.timestamp() - reserved_at >= _QUEUE_RESERVATION_GRACE_SECONDS
        ):
            reservations.pop(slot_key, None)
            continue
        occupied.append(reserved_slot)

    slot = post_queue.next_available_slot(
        now,
        occupied,
    )
    reservations[post_queue._slot_key(slot)] = (slot, now.timestamp())
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
                reservations = _queue_reservations.get(target_key)
                if reservations is not None:
                    reservations.pop(post_queue._slot_key(schedule_time), None)
                    if not reservations:
                        _queue_reservations.pop(target_key, None)
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


async def _post_price_changes(farsi_text: str | list, fallers: list | None = None):
    """Post a complete price update or the partial update released by its timer."""
    if not isinstance(farsi_text, str):
        farsi_text = price_changes.format_price_changes_farsi(farsi_text, fallers or [])
    await _send_to_target(farsi_text)
    logger.info("Posted price changes to %s", _target_channel())


async def _send_alert(farsi_text: str, event):
    await _send_to_target(farsi_text, event=event)
    logger.info("Sent game alert to %s", _target_channel())
    await _send_notification(event, farsi_text)


async def _try_handle_automatic_content(text: str, event) -> bool:
    """Format recognised FPL source posts without an LLM or scheduling delay."""
    if alerts.is_game_alert(text):
        parsed = alerts.parse(text)
        if parsed:
            farsi = alerts.format_farsi(parsed)
            if farsi:
                logger.info("Detected game-action alert, formatting directly")
                await _send_alert(farsi, event)
                return True

    if alerts.is_lineup(text):
        parsed = alerts.parse_lineup(text)
        if parsed:
            farsi = alerts.format_lineup(parsed)
            if farsi:
                logger.info("Detected lineup, formatting directly")
                await _send_alert(farsi, event)
                return True

    if price_changes.is_price_change(text):
        parsed = price_changes.parse_price_change(text)
        if parsed:
            logger.info(
                "Detected price change: %s (%d players)",
                parsed.change_type, len(parsed.players),
            )
            combined = price_changes.accumulate(parsed, _post_price_changes)
            if combined:
                await _post_price_changes(combined)
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
        if alerts.is_game_alert(raw_text):
            parsed = alerts.parse(raw_text)
            if parsed:
                caption = alerts.format_farsi(parsed) or raw_text
        elif alerts.is_lineup(raw_text):
            parsed = alerts.parse_lineup(raw_text)
            if parsed:
                caption = alerts.format_lineup(parsed) or raw_text
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


async def handle_new_message(event):
    _refresh_translator_model()
    text = event.message.text
    media = event.message.media
    grouped_id = event.message.grouped_id

    if not text and not media:
        return

    # These source formats must stay out of the generic chunk/LLM pipeline:
    # price risers and fallers arrive as separate messages, while lineups and
    # live alerts need their database-backed Farsi formatting immediately.
    if text and not grouped_id and await _try_handle_automatic_content(text, event):
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
                result = await translator.translate_article(html)
                title = _article_title(result.get("title", ""))
                summary = result.get("summary", "")
                body = _fix_unclosed_tags(_strip_quotes(result.get("body", "")))
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
            result = await translator.translate_article(html)
            title = _article_title(result.get("title", ""))
            summary = result.get("summary", "")
            body = _fix_unclosed_tags(_strip_quotes(result.get("body", "")))
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
    result = await translator.translate_article(translation_html)
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
    translated = articles.restore_images_in_place(
        translated, source_images, source_html=translation_html
    )
    translated = articles.append_original_article_link(translated, article["url"])

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
            source_name=article.get("source_name", ""),
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
    _refresh_translator_model()
    translated_title = youtube_posts.clean_video_title(
        _fix_unclosed_tags(
            _strip_quotes(await translator.translate(_escape_html(metadata.title)))
        )
    )
    if len(transcript) <= _YOUTUBE_INLINE_TRANSCRIPT_LIMIT:
        article = await translator.translate_article(
            _escape_html(transcript), transcript=True
        )
        translated_transcript = _telegraph_to_telegram_html(
            _fix_unclosed_tags(_strip_quotes(article.get("body", "")))
        )
        if not translated_transcript.strip():
            raise RuntimeError("ترجمهٔ زیرنویس خالی بود.")
        inline_caption = _format_youtube_inline_post(
            translated_title,
            translated_transcript,
            url,
            metadata.channel_title,
        )
        # Providers can expand text substantially.  Keep the post usable by
        # falling back to the long-form pipeline instead of exceeding Telegram's
        # photo-caption limit.
        if len(_strip_html_tags(inline_caption)) <= 1000:
            thumbnail = await asyncio.to_thread(
                _download_remote_media, metadata.thumbnail_url, "photo"
            )
            try:
                await _send_to_target(
                    inline_caption, file_path=thumbnail,
                    queue=True,
                )
                await _send_notification(
                    None, inline_caption, source="YouTube", is_media=True,
                )
            finally:
                Path(thumbnail).unlink(missing_ok=True)
            return (
                f"✅ متن فارسی کوتاه برای بررسی، {QUEUED_POST_CONFIRMATION}"
            )

    article = await translator.translate_article(_escape_html(transcript), transcript=True)
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
    article_body = _fix_unclosed_tags(_strip_quotes(article.get("body", "")))
    if not article_body.strip():
        raise RuntimeError("ترجمهٔ زیرنویس خالی بود.")
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
        metadata.channel_title,
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


async def _translate_dashboard_submission(event) -> str:
    """Translate an admin's private bot submission through the normal pipeline."""
    _refresh_translator_model()
    text = event.message.text or ""
    html = _strip_quotes(_strip_hashtags(_message_to_html(text, event.message.entities)))
    links = _extract_urls(event)
    link_url = await _display_link_url(links)
    for url in links:
        html = html.replace(url, "")
    html = html.strip()

    if html and len(_strip_html_tags(html)) > _ARTICLE_SOURCE_THRESHOLD:
        result = await translator.translate_article(html)
        title = _article_title(_fix_unclosed_tags(_strip_quotes(result.get("title", ""))))
        summary = _fix_unclosed_tags(_strip_quotes(result.get("summary", "")))
        body = _fix_unclosed_tags(_strip_quotes(result.get("body", "")))
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
    return await asyncio.to_thread(article_catalog.list_pages, limit=50)


async def _enrich_article_catalog() -> None:
    """Backfill images and AI summaries for imported Telegraph pages."""
    import article_catalog

    if not os.getenv("TELEGRAPH_ACCESS_TOKEN"):
        return
    try:
        await asyncio.to_thread(article_catalog.sync_from_telegraph)
        pages = await asyncio.to_thread(
            article_catalog.pages_needing_enrichment, 50
        )
        for page in pages:
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
                                or next(
                                    iter(source_article.get("images", [])), ""
                                )
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
    except Exception:
        logger.exception("Telegraph catalog enrichment failed")


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


def _fixtures_text() -> str:
    gameweek = db.query_one(
        "SELECT id, name FROM gameweeks WHERE is_current=1 OR is_next=1 "
        "ORDER BY CASE WHEN is_current=1 THEN 0 ELSE 1 END, id LIMIT 1"
    )
    if not gameweek:
        raise RuntimeError("گیم‌ویک فعلی یا بعدی پیدا نشد.")
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
        day = kickoff.strftime("%Y/%m/%d")
        if day != current_day:
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
            "ترکیب‌ها به‌صورت خودکار از کانال‌های منبع دریافت، با نام و قیمت فارسی "
            "فرمت و بدون تأخیر منتشر می‌شوند. برای این بخش انتشار دستی لازم نیست."
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
    client.add_event_handler(_handle_configured_source_message, events.NewMessage(incoming=True))
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

    tasks = [
        _start_health_server(),
        _enrich_article_catalog(),
        article_monitor.run_monitor(_publish_article_from_url),
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
