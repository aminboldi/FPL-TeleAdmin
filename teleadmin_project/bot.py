import asyncio
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityTextUrl,
)

from config import load_config
from translator import Translator, TranslationError
import alerts
import price_changes
import deadlines
import articles
import database as db
import scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("TeleAdmin")

settings = load_config()

translator = Translator(
    api_key=settings.openrouter_api_key,
    model=settings.openrouter_model,
    fallback_model=settings.fallback_model,
)

_session_string = os.getenv("TELETHON_SESSION_STRING")
client = TelegramClient(
    StringSession(_session_string) if _session_string else "translation_session",
    settings.telegram_api_id,
    settings.telegram_api_hash,
)


_SOURCE_CHANNELS = [
    c for c in (settings.source_channel_id, settings.source_channel2_id) if c
]

SIGNATURE = "@EPL_Fantasy"
AI_SIGNATURE = "@EPL_Fantasy | \u2728AI"
SCHEDULE_DELAY_MINUTES = 10
_ALBUM_TIMEOUT = 5
_ARTICLE_SOURCE_THRESHOLD = 350
_CHUNK_TIMEOUT = 3  # seconds to wait for text chunks from same chat

_album_buffer: dict[int, list] = {}
_album_tasks: dict[int, asyncio.Task] = {}
_album_caption: dict[int, str] = {}

_chunk_buffer: dict[int, list] = {}
_chunk_tasks: dict[int, asyncio.Task] = {}


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


def _format_telegraph_post(title: str, summary: str, telegraph_url: str) -> str:
    return (
        f"<b>✍ مقاله:</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"- - - - - - - - -\n\n"
        f"{summary}\n\n"
        f'<a href="{telegraph_url}">متن کامل مقاله: 👇👇👇</a>'
        f"\n\n{AI_SIGNATURE}"
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


def _clean_text(text: str, event=None) -> tuple[str | None, str | None]:
    text = _strip_hashtags(text)
    urls = _extract_urls(event) if event else []
    link_url = urls[0] if urls else None
    for url in urls:
        text = text.replace(url, "")
    text = text.strip() or None
    return text, link_url


def _build_caption(
    translated: str | None, *, link_url: str | None = None, html: bool = False
) -> str:
    parts = []
    if translated:
        if html:
            parts.append(translated)
        else:
            parts.append(_format_numbers(translated))
    if link_url:
        parts.append(f'<a href="{link_url}">لینک</a>')
    if not parts:
        return AI_SIGNATURE
    return "\n\n".join(parts + [AI_SIGNATURE])


def _media_suffix(event) -> str:
    if event.message.file and event.message.file.ext:
        return f".{event.message.file.ext}"
    return ""


async def _send_notification(event, caption: str):
    if not settings.notif_channel_id:
        return

    source = (
        getattr(event.chat, "title", None)
        or getattr(event.chat, "username", None)
        or str(event.chat_id)
    )

    preview = caption
    if len(preview) > 300:
        preview = preview[:300] + "..."

    media_tag = "Media" if event.message.media else "Text"
    notif = (
        f"<b>[{media_tag}] New post</b>\n"
        f"<b>Source:</b> {source}\n\n"
        f"{preview}"
    )

    await client.send_message(
        settings.notif_channel_id,
        notif,
        parse_mode="html",
    )


async def _send_to_target(text: str, *, event=None, file_path=None, album_paths=None, is_album=False, schedule_minutes: int = 0):
    reply_to = _get_reply_to(event) if event else None
    schedule_time = datetime.now(tz=timezone.utc) + timedelta(minutes=schedule_minutes) if schedule_minutes else None
    try:
        if album_paths:
            msg = await client.send_file(
                settings.target_channel_id,
                album_paths,
                caption=text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        elif file_path:
            msg = await client.send_file(
                settings.target_channel_id,
                file_path,
                caption=text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        else:
            msg = await client.send_message(
                settings.target_channel_id,
                text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        if event:
            _save_mapping(event, msg.id)
        return msg
    except FloodWaitError as e:
        logger.warning("FloodWaitError: sleeping %ss", e.seconds)
        await asyncio.sleep(e.seconds)
        if album_paths:
            msg = await client.send_file(
                settings.target_channel_id,
                album_paths,
                caption=text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        elif file_path:
            msg = await client.send_file(
                settings.target_channel_id,
                file_path,
                caption=text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        else:
            msg = await client.send_message(
                settings.target_channel_id,
                text,
                reply_to=reply_to,
                parse_mode="html",
                schedule=schedule_time,
            )
        if event:
            _save_mapping(event, msg.id)
        return msg


async def _post_price_changes(farsi_text: str):
    await _send_to_target(farsi_text)
    logger.info("Posted price changes to %s", settings.target_channel_id)


async def _send_alert(farsi_text: str, event):
    await _send_to_target(farsi_text, event=event)
    logger.info("Sent game alert to %s", settings.target_channel_id)
    await _send_notification(event, farsi_text)


async def _forward_message(caption: str, event):
    media = event.message.media
    if media:
        await _forward_media(caption, event)
    else:
        await _send_to_target(caption, event=event, schedule_minutes=SCHEDULE_DELAY_MINUTES)


async def _forward_media(caption: str, event):
    media = event.message.media
    if media:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=_media_suffix(event))
        try:
            temp.close()
            await event.message.download_media(file=temp.name)
            await _send_to_target(caption, file_path=temp.name, event=event, schedule_minutes=SCHEDULE_DELAY_MINUTES)
        finally:
            os.unlink(temp.name)


async def _forward_album(caption: str, events: list):
    temps = []
    try:
        for evt in events:
            if evt.message.media:
                temp = tempfile.NamedTemporaryFile(
                    delete=False, suffix=_media_suffix(evt)
                )
                temp.close()
                await evt.message.download_media(file=temp.name)
                temps.append(temp.name)

        if not temps:
            return

        if len(temps) == 1:
            await _send_to_target(caption, file_path=temps[0], event=events[0], schedule_minutes=SCHEDULE_DELAY_MINUTES)
        else:
            await _send_to_target(caption, album_paths=temps, event=events[0], schedule_minutes=SCHEDULE_DELAY_MINUTES)
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
                link_url = links[0] if links else None
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


@client.on(events.NewMessage(chats=_SOURCE_CHANNELS))
async def handle_new_message(event):
    text = event.message.text
    media = event.message.media
    grouped_id = event.message.grouped_id

    if not text and not media:
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

    if text and alerts.is_game_alert(text):
        parsed = alerts.parse(text)
        if parsed:
            farsi = alerts.format_farsi(parsed)
            if farsi:
                logger.info("Detected game-action alert, formatting directly")
                await _send_alert(farsi, event)
                return

    if text and alerts.is_lineup(text):
        parsed = alerts.parse_lineup(text)
        if parsed:
            farsi = alerts.format_lineup(parsed)
            if farsi:
                logger.info("Detected lineup, formatting directly")
                await _send_alert(farsi, event)
                return

    if text and price_changes.is_price_change(text):
        parsed = price_changes.parse_price_change(text)
        if parsed:
            logger.info(
                "Detected price change: %s (%d players)",
                parsed.change_type, len(parsed.players),
            )
            combined = price_changes.accumulate(parsed, _post_price_changes)
            if combined:
                await _post_price_changes(combined)
            return

    html = _message_to_html(text or "", event.message.entities)
    html = _strip_hashtags(html)
    html = _strip_quotes(html)
    links = _extract_urls(event)
    link_url = links[0] if links else None
    for url in links:
        html = html.replace(url, "")
    html = html.strip()

    translated = None
    if html:
        try:
            if len(_strip_html_tags(html)) > _ARTICLE_SOURCE_THRESHOLD:
                result = await translator.translate_article(html)
                title = result.get("title", "")
                summary = result.get("summary", "")
                body = _fix_unclosed_tags(_strip_quotes(result.get("body", "")))
                telegraph_url = articles.publish_to_telegraph(title, body)
                if telegraph_url:
                    caption = _format_telegraph_post(title, summary, telegraph_url)
                    await _send_to_target(caption, event=event, schedule_minutes=SCHEDULE_DELAY_MINUTES)
                    await _send_notification(event, caption)
                    logger.info("Published Telegraph article (%d chars)", len(body))
                else:
                    caption = _build_caption(body, link_url=link_url, html=True)
                    await _forward_message(caption, event)
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
        logger.info("Forwarded message to %s", settings.target_channel_id)
        await _send_notification(event, caption)

    await _maybe_post_article(text or "", event)


async def _finish_chunks(chat_id: int):
    await asyncio.sleep(_CHUNK_TIMEOUT)
    chunks = _chunk_buffer.pop(chat_id, [])
    _chunk_tasks.pop(chat_id, None)
    if not chunks:
        return

    merged_text = "\n".join(evt.message.text for evt in chunks if evt.message.text)
    first_evt = chunks[0]
    logger.info("Merged %d text chunks from chat %d (%d chars)", len(chunks), chat_id, len(merged_text))

    html = _message_to_html(merged_text, first_evt.message.entities)
    html = _strip_hashtags(html)
    html = _strip_quotes(html)
    links = _extract_urls(first_evt)
    link_url = links[0] if links else None
    for url in links:
        html = html.replace(url, "")
    html = html.strip()

    translated = None
    if html:
        try:
            result = await translator.translate_article(html)
            title = result.get("title", "")
            summary = result.get("summary", "")
            body = _fix_unclosed_tags(_strip_quotes(result.get("body", "")))
            telegraph_url = articles.publish_to_telegraph(title, body)
            if telegraph_url:
                caption = _format_telegraph_post(title, summary, telegraph_url)
                await _send_to_target(caption, event=first_evt, schedule_minutes=SCHEDULE_DELAY_MINUTES)
                await _send_notification(first_evt, caption)
            else:
                caption = _build_caption(body, link_url=link_url, html=True)
                await _forward_message(caption, first_evt)
        except TranslationError as e:
            logger.error("Translation error for chunks: %s", e)
            return
        except Exception as e:
            logger.error("Unexpected translation error for chunks: %s", e)
            return

    await _maybe_post_article(merged_text, first_evt)


async def _maybe_post_article(text: str, event):
    url = None
    for m in re.finditer(r"(?:https?://)?\S+", text):
        raw = m.group(0)
        if articles.is_pl_article_url(raw):
            url = articles.resolve_url(raw)
            break
    if not url and event.message.entities:
        for e in event.message.entities:
            u = getattr(e, "url", None)
            if u and articles.is_pl_article_url(u):
                url = u
                break
    if not url:
        return

    logger.info("Post-processing article URL: %s", url)
    article = articles.fetch_article(url)

    logger.info("Post-processing article URL: %s", url)
    article = articles.fetch_article(url)
    if not article or not article.get("parts"):
        logger.warning("Could not extract article from %s", url)
        return

    raw_html = articles.build_article_html(
        article["title"], article["date"], article["summary"],
        article["parts"], article["url"], article.get("header_image", ""),
    )
    try:
        translated = _fix_unclosed_tags(
            _strip_quotes(await translator.translate(raw_html))
        )
        telegraph_url = articles.publish_to_telegraph(article["title"], translated)
        if telegraph_url:
            summary = article.get("summary", "")
            if not summary and article.get("parts"):
                for p in article["parts"]:
                    if p["type"] == "p":
                        summary = p["text"][:300]
                        break
            caption = _format_telegraph_post(
                article["title"], _strip_html_tags(summary), telegraph_url
            )
            await _send_to_target(caption, event=event, schedule_minutes=SCHEDULE_DELAY_MINUTES)
    except Exception as e:
        logger.error("Article post-processing error: %s", e)


async def _health_handler(reader, writer):
    try:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
    finally:
        writer.close()


async def _start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = await asyncio.start_server(_health_handler, "0.0.0.0", port)
    logger.info("Health server listening on port %s", port)
    async with server:
        await server.serve_forever()


async def main():
    logger.info("Starting TeleAdmin bot...")
    logger.info("  Sources: %s", ", ".join(_SOURCE_CHANNELS))
    logger.info("  Target : %s", settings.target_channel_id)
    if settings.notif_channel_id:
        logger.info("  Notif  : %s", settings.notif_channel_id)
    logger.info("  Model  : %s", settings.openrouter_model)

    await client.start()
    logger.info("Bot is running. Press Ctrl+C to stop.")

    await asyncio.gather(
        _start_health_server(),
        client.run_until_disconnected(),
        deadlines.run_deadline_loop(
            client=client,
            target_channel=settings.target_channel_id,
            league_code=settings.league_code,
        ),
        scheduler.run_scheduler(
            client=client,
            target_channel=settings.target_channel_id,
            league_code=settings.league_code,
        ),
    )


if __name__ == "__main__":
    client.loop.run_until_complete(main())
