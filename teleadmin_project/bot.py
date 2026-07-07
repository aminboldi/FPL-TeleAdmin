import asyncio
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from config import load_config
from translator import Translator, TranslationError

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

client = TelegramClient(
    "translation_session",
    settings.telegram_api_id,
    settings.telegram_api_hash,
)


SIGNATURE = "@EPL_Fantasy"
SCHEDULE_DELAY_MINUTES = 10

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


def _build_caption(
    translated: str | None, *, link_url: str | None = None
) -> str:
    parts = []
    if translated:
        parts.append(_format_numbers(translated))
    if link_url:
        parts.append(f'<a href="{link_url}">لینک</a>')
    if not parts:
        return SIGNATURE
    return "\n\n".join(parts + [SIGNATURE])


def _media_suffix(event) -> str:
    if event.message.file and event.message.file.ext:
        return f".{event.message.file.ext}"
    return ""


async def _forward_message(caption: str, event):
    media = event.message.media
    schedule_time = datetime.now(tz=timezone.utc) + timedelta(
        minutes=SCHEDULE_DELAY_MINUTES
    )

    if media:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=_media_suffix(event))
        try:
            temp.close()
            await event.message.download_media(file=temp.name)
            await client.send_file(
                settings.target_channel_id,
                temp.name,
                caption=caption,
                schedule=schedule_time,
                parse_mode="html",
            )
        finally:
            os.unlink(temp.name)
    else:
        await client.send_message(
            settings.target_channel_id,
            caption,
            schedule=schedule_time,
            parse_mode="html",
        )


@client.on(events.NewMessage(chats=[settings.source_channel_id]))
async def handle_new_message(event):
    text = event.message.text
    media = event.message.media

    if not text and not media:
        return

    link_url = None
    if text:
        text = _strip_hashtags(text)
        urls = _extract_urls(event)
        if urls:
            link_url = urls[0]
            for url in urls:
                text = text.replace(url, "")
        text = text.strip() or None

    translated = None
    if text:
        try:
            translated = await translator.translate(text)
        except TranslationError as e:
            logger.error("Translation error: %s", e)
            return
        except Exception as e:
            logger.error("Unexpected translation error: %s", e)
            return

    caption = _build_caption(translated, link_url=link_url)

    try:
        await _forward_message(caption, event)
        logger.info("Forwarded message to %s", settings.target_channel_id)
    except FloodWaitError as e:
        logger.warning("FloodWaitError: sleeping %ss", e.seconds)
        await asyncio.sleep(e.seconds)
        await _forward_message(caption, event)
    except Exception as e:
        logger.error("Failed to send message: %s", e)


async def main():
    logger.info("Starting TeleAdmin bot...")
    logger.info("  Source : %s", settings.source_channel_id)
    logger.info("  Target : %s", settings.target_channel_id)
    logger.info("  Model  : %s", settings.openrouter_model)

    await client.start()
    logger.info("Bot is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
