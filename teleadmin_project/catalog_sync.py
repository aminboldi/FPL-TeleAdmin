"""Keep the public article catalog in step with the channel: visibility of
published posts and the one-off enrichment sweep over older pages."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse



import articles
from app import (
    _target_channel,
    client,
    logger,
    translator,
)
from post_format import (
    _extract_urls_from_message,
    _youtube_thumbnail_url,
)


_CATALOG_VISIBILITY_LOOKBACK = timedelta(days=3)


_CATALOG_ENRICHMENT_BATCH = 25


# One start's worth of backlog. Each page costs a Telegraph fetch, and pages
# that recover nothing are stamped so the next start continues past them.
_CATALOG_ENRICHMENT_LIMIT = 500


_CATALOG_ENRICHMENT_DELAY = 0.5


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
