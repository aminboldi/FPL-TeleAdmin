"""URL articles: fetch, translate, publish to Telegraph or inline, and queue."""
import asyncio
from pathlib import Path



from translator import ContentIncompleteError
import articles
import database as db
import telegraph_editor
import runtime_config
from app import (
    QUEUED_POST_CONFIRMATION,
    _refresh_translator_model,
    logger,
    translator,
)
from post_format import (
    _MEDIA_CAPTION_LIMIT,
    _article_title,
    _caption_length,
    _escape_html,
    _extract_urls,
    _fix_unclosed_tags,
    _format_short_article_post,
    _format_telegraph_post,
    _rendered_text_length,
    _strip_quotes,
)
from delivery import (
    _download_remote_media,
    _send_notification,
    _send_to_target,
)


async def _notify_article_abandoned(url: str, error: Exception):
    """Alert admins when a monitored article stayed incomplete after retries."""
    caption = (
        "<b>⚠️ این مقاله ناقص دریافت شد و منتشر نشد</b>\n\n"
        f'<a href="{_escape_html(url)}">مشاهدهٔ مقاله</a>\n\n'
        f"{_escape_html(str(error))}"
    )
    await _send_notification(None, caption, source="Article", is_media=False)


async def _maybe_post_article(text: str, event, *, urls=None):
    urls = list(dict.fromkeys(urls if urls is not None else _extract_urls(event)))
    urls = [url for url in urls if url.lower().startswith(("http://", "https://"))]

    # Telegram gives scheduled posts IDs from a separate queue. Wait for the
    # original to publish before creating a reply using its real channel ID.
    mapping = db.lookup_message_mapping(event.chat_id, event.message.id)
    if mapping and mapping["target_channel"]:
        for url in urls:
            runtime_config.enqueue_source_article(event.chat_id, event.message.id, url)
        return

    for url in urls:
        logger.info("Post-processing possible article URL: %s", url)
        try:
            posted = await _publish_article_from_url(url, event=event)
        except Exception as exc:
            logger.info("Article post-processing error for %s: %s", url, exc)
            continue
        if posted:
            return


async def _publish_article_from_url(url: str, *, event=None, deliver_post=None) -> bool:
    """Translate an article, then use its delivery route or the review queue."""
    async def send_post(caption, *, file_path):
        if deliver_post is not None:
            await deliver_post(caption, file_path=file_path)
        else:
            await _send_to_target(caption, file_path=file_path, event=event, queue=True)

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
                await send_post(caption, file_path=feature_path)
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
        await send_post(caption, file_path=feature_path)
        if event:
            await _send_notification(event, caption, is_media=True)
        else:
            await _send_notification(None, caption, source="Article", is_media=True)
    finally:
        Path(feature_path).unlink(missing_ok=True)
    logger.info("Published article from %s", url)
    return True


async def _telegraph_editor_url(article_url: str) -> str:
    return telegraph_editor.create_edit_url(article_url)


async def _recent_telegraph_pages() -> list[dict]:
    import article_catalog

    await asyncio.to_thread(article_catalog.sync_from_telegraph)
    # The private editor must also show queued (not-yet-public) articles.
    return await asyncio.to_thread(
        article_catalog.list_pages, limit=50, include_hidden=True
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
