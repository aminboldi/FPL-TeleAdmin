import asyncio
import os
from urllib.parse import urlparse


from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    Channel,
    UpdateDeleteScheduledMessages,
    User,
)

from translator import TranslationError
import alerts
import deadlines
import articles
import article_monitor
import article_relay
import source_articles
import fplcore
import database as db
import scheduler
import telegraph_editor
import runtime_config
import youtube_monitor
import player_names
import translate_names
from admin_dashboard import AdminDashboard
from app import (
    _refresh_translator_model,
    _target_channel,
    client,
    logger,
    settings,
    translator,
)
from post_format import (
    _ARTICLE_SOURCE_THRESHOLD,
    _article_title,
    _build_caption,
    _catalog_source_tag,
    _display_link_url,
    _escape_html,
    _extract_urls,
    _fix_unclosed_tags,
    _format_telegraph_post,
    _message_to_html,
    _prepare_article_source,
    _strip_hashtags,
    _strip_html_tags,
    _strip_quotes,
    _strip_visible_urls,
)
import delivery
from delivery import (
    _forward_album,
    _forward_message,
    _handle_scheduled_publication,
    _log_effective_settings,
    _notify_admin,
    _send_notification,
    _send_source_article_reply,
    _send_to_target,
    _send_to_target_at,
    _try_handle_automatic_content,
)
from catalog_sync import (
    _enrich_article_catalog,
    _handle_target_channel_message,
    _sync_catalog_visibility_from_target,
)
from article_pipeline import (
    _article_catalog_url,
    _import_article,
    _maybe_post_article,
    _notify_article_abandoned,
    _publish_article_from_url,
    _recent_telegraph_pages,
    _telegraph_editor_url,
)
from youtube_pipeline import (
    _import_youtube_transcript,
    _notify_youtube_monitor_failure,
)
from x_pipeline import (
    _prepare_x_post,
)
from forwarded import (
    _translate_dashboard_submission,
)
from dashboard_content import (
    _openrouter_balance,
    _prepare_dashboard_content,
    _publish_dashboard_content,
)


_ALBUM_TIMEOUT = 5


# Media captions are limited to 1,024 rendered characters. Keep the short
# article heuristic conservative because Persian translation can expand and
# the caption also contains the article header, source link, and signature.
# Not everything needs a Telegraph page. A translation that fits in a caption,
# with no inline image to place, is published as an ordinary post carrying its
# feature image. There is no separate length limit on the body: the caption
# either holds the whole thing or the article goes to Telegraph, so nothing is
# ever trimmed to make it fit.
_CHUNK_TIMEOUT = 3  # seconds to wait for text chunks from same chat


_AUTOMATIC_ONLY_SOURCE_REFS = ("@FPLFootball",)


_album_buffer: dict[int, list] = {}


_album_tasks: dict[int, asyncio.Task] = {}


_album_caption: dict[int, str] = {}


_chunk_buffer: dict[int, list] = {}


_chunk_tasks: dict[int, asyncio.Task] = {}


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


async def _fill_missing_player_names() -> int:
    last_error = None
    for api_client, model in (
        (translator.google_client, translator.google_model),
        (translator.openrouter_client, translator.fallback_model),
    ):
        if api_client is None:
            continue
        try:
            return await translate_names.fill_missing_names(api_client, model, model)
        except Exception as exc:
            last_error = exc
            logger.warning("Missing player-name translation failed with %s: %s", model, exc)
    raise TranslationError("Could not fill missing Persian player names") from last_error


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
                html = _strip_visible_urls(html, html=True)
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
    html = _strip_visible_urls(html, html=True)

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

    html = "\n".join(
        _message_to_html(evt.message.text, evt.message.entities)
        for evt in chunks if evt.message.text
    )
    html = _strip_hashtags(html)
    html = _strip_quotes(html)
    links = list(dict.fromkeys(url for evt in chunks for url in _extract_urls(evt)))
    link_url = await _display_link_url(links)
    html = _strip_visible_urls(html, html=True)

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
                    source_tag=_catalog_source_tag(first_evt),
                )
                if telegraph_url:
                    caption = _format_telegraph_post(title, summary, telegraph_url)
                    await _send_to_target(caption, event=first_evt, queue=True)
                else:
                    caption = _build_caption(body, link_url=link_url, html=True)
                    await _forward_message(caption, first_evt)
            else:
                translated = _fix_unclosed_tags(_strip_quotes(await translator.translate(html)))
                caption = _build_caption(translated, link_url=link_url, html=True)
                await _forward_message(caption, first_evt)
            await _send_notification(first_evt, caption)
        except TranslationError as e:
            logger.error("Translation error for chunks: %s", e)
            return
        except Exception as e:
            logger.error("Unexpected translation error for chunks: %s", e)
            return

    await _maybe_post_article(merged_text, first_evt, urls=links)


async def _start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = await asyncio.start_server(telegraph_editor.handle_http, "0.0.0.0", port)
    logger.info("Health server listening on port %s", port)
    async with server:
        await server.serve_forever()


async def main():
    logger.info("Starting TeleAdmin bot...")
    logger.info("  Target : %s", _target_channel())
    logger.info("  Model  : %s", runtime_config.get("OPEN_ROUTER_MODEL"))

    await client.start()
    await _ensure_automatic_sources()
    client.add_event_handler(_handle_configured_source_message, events.NewMessage(incoming=True))
    client.add_event_handler(_handle_scheduled_publication, events.Raw(types=UpdateDeleteScheduledMessages))
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
        # delivery reads this when it sends admin notifications.
        delivery._admin_bot_client = admin_client
        logger.info("Admin dashboard enabled for %d user(s)", len(settings.admin_user_ids))
    else:
        logger.warning("Admin dashboard disabled: set TELEGRAM_BOT_TOKEN and ADMIN_USER_IDS")

    _log_effective_settings()

    # A short-lived feature guessed a player's Persian spelling from nearby
    # words and recorded ordinary words as names, which then rewrote those
    # words in every later translation. Remove what it wrote before the index
    # is built from it.
    purged = await asyncio.to_thread(db.purge_guessed_aliases)
    if purged:
        logger.warning(
            "Removed %d guessed player alias(es): %s",
            len(purged),
            ", ".join(f"{alias!r} on {name}" for name, alias in purged[:20]),
        )

    # Build the player-name index before the first message arrives, so no
    # translation pays for it.
    await asyncio.to_thread(player_names.reload)

    tasks = [
        _start_health_server(),
        _enrich_article_catalog(),
        source_articles.SourceArticles(
            client, _publish_article_from_url, _send_source_article_reply,
        ).run(),
        article_monitor.run_monitor(
            _publish_article_from_url, _notify_article_abandoned,
            import_fplarmo=article_relay.ArticleRelay(
                client, fplcore.CHANNEL, _target_channel,
                _publish_article_from_url, _send_to_target_at,
            ).publish,
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
            notify=_notify_admin,
            enrich_player_names=_fill_missing_player_names,
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
