"""Sending to the target channel: the review queue, immediate sends, alerts,
media forwarding, reply mapping and admin notifications."""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from telethon import TelegramClient
from telethon.errors import FloodWaitError

import alerts
import price_changes
import database as db
import post_queue
import runtime_config
from app import (
    _target_channel,
    client,
    logger,
    settings,
)
from post_format import (
    _has_uploadable_media,
    _media_suffix,
)


_admin_bot_client: TelegramClient | None = None


_schedule_slot_lock = asyncio.Lock()


# Telegram can briefly omit a message immediately after it is scheduled. Keep
# the last allocated slot locally so successive posts advance even when the
# scheduled-message history has not caught up yet. The history remains the
# restart-safe source of truth because this cursor only lives in memory.
_last_reserved_queue_slots: dict[str, datetime] = {}


_automatic_alert_lock = asyncio.Lock()


def _get_reply_to(event) -> int | None:
    if not event.message.reply_to:
        return None
    reply_msg_id = event.message.reply_to.reply_to_msg_id
    if not reply_msg_id:
        return None
    return db.lookup_target_msg(event.chat_id, reply_msg_id)


def _save_mapping(event, message, target_channel, schedule_time) -> None:
    db.store_message_mapping(
        event.chat_id, event.message.id, message.id,
        is_scheduled=schedule_time is not None,
        target_channel=str(target_channel),
        target_text=message.message or "",
        scheduled_at=schedule_time.isoformat() if schedule_time else "",
    )


async def _handle_scheduled_publication(update) -> None:
    if not update.sent_messages:
        return
    try:
        target = _target_channel()
        peer = await client.get_input_entity(target)
        if getattr(peer, "channel_id", None) != getattr(update.peer, "channel_id", None):
            return
        for scheduled_id, published_id in zip(update.messages, update.sent_messages):
            db.promote_scheduled_mapping(str(target), scheduled_id, published_id)
    except Exception:
        logger.exception("Could not record published scheduled-message IDs")


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


def _log_effective_settings() -> None:
    """Log which databases this instance uses and what they say.

    Two deployments pointed at one persistent volume share these files, and
    therefore share every setting, post marker, and price baseline in them.
    That is how a watchlist can be disabled for a deployment whose own
    environment enables it: whichever instance created the settings row first
    decided the value for both. Printing the paths and the effective values at
    boot turns that from a multi-day hunt into one look at the deploy log.
    """
    logger.info("Runtime settings database: %s", runtime_config.DB_PATH)
    logger.info("FPL database: %s", db.DB_PATH)
    for key, value in sorted(runtime_config.values().items()):
        environment = runtime_config._env_value(key)
        note = ""
        if environment is not None and environment != value:
            note = f"  (environment says {environment!r})"
        logger.info("  %s = %r%s", key, value, note)


async def _notify_admin(caption: str) -> None:
    """Send a scheduler-level warning to the private admin chat."""
    await _send_notification(None, caption, source="Scheduler", is_media=False)


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
    queue: bool = False, target_channel=None, reply_to=None,
):
    target_channel = target_channel or _target_channel()
    if not target_channel:
        raise RuntimeError("TARGET_CHANNEL_ID is not configured")
    if reply_to is None and event:
        reply_to = _get_reply_to(event)
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


async def _send_source_article_reply(target_channel, parent_id, caption, *, file_path=None):
    return await _send_to_target(
        caption, file_path=file_path, queue=True,
        target_channel=target_channel, reply_to=parent_id,
    )


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
            _save_mapping(event, mapped_message, target_channel, schedule_time)
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
            _save_mapping(event, mapped_message, target_channel, schedule_time)
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
