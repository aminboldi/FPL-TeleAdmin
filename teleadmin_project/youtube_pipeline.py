"""YouTube videos: transcript, translation, and the channel post."""
import asyncio
from pathlib import Path



from translator import ContentIncompleteError, VIDEO_TITLE_INSTRUCTIONS
import articles
import runtime_config
import youtube_posts
import player_names
from app import (
    QUEUED_POST_CONFIRMATION,
    _refresh_translator_model,
    logger,
    settings,
    translator,
)
from post_format import (
    _MEDIA_CAPTION_LIMIT,
    _article_title,
    _caption_length,
    _escape_html,
    _fix_unclosed_tags,
    _format_youtube_inline_post,
    _format_youtube_telegraph_post,
    _rendered_text_length,
    _strip_quotes,
    _telegraph_to_telegram_html,
    _youtube_description_preview,
)
from delivery import (
    _download_remote_media,
    _send_notification,
    _send_to_target,
)


_youtube_failure_notified: set[str] = set()


_youtube_incomplete_attempts: dict[str, int] = {}


# Captions are often still being generated shortly after upload, so a partial
# transcript is retried across polls before the video is skipped.
_MAX_INCOMPLETE_TRANSCRIPT_ATTEMPTS = 4


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
    creator = youtube_posts.creator_name(metadata.channel_title)
    article = await translator.translate_article(
        _escape_html(transcript), transcript=True, speaker=creator,
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
        creator=creator,
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
        creator=creator,
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
