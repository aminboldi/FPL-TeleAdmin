"""Posts an admin forwards to the private bot: single posts and merged batches."""
import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path



import articles
from app import (
    QUEUED_POST_CONFIRMATION,
    _refresh_translator_model,
    logger,
    translator,
)
from post_format import (
    AI_SIGNATURE,
    _ARTICLE_SOURCE_THRESHOLD,
    _MEDIA_CAPTION_LIMIT,
    _article_title,
    _build_caption,
    _display_link_url,
    _extract_urls,
    _fix_unclosed_tags,
    _format_telegraph_post,
    _has_uploadable_media,
    _media_suffix,
    _message_to_html,
    _prepare_article_source,
    _prepare_plain_article_layout,
    _rendered_text_length,
    _strip_hashtags,
    _strip_html_tags,
    _strip_quotes,
    _strip_visible_urls,
)
from delivery import (
    _forward_message,
    _send_notification,
    _send_to_target,
    _try_handle_automatic_content,
)


# A quiet window is the only thing holding a forwarded burst together, so it has
# to outlast the slowest post in it. Three seconds was short enough that a post
# arriving behind the others — in practice one carrying a photo — missed the
# batch and was published on its own. Once the batch contains media it waits
# longer still, because that is when arrival is most uneven.
_FORWARDED_BATCH_TIMEOUT = 6


_FORWARDED_BATCH_MEDIA_TIMEOUT = 12


_forward_batch_buffer: dict[int, list] = {}


_forward_batch_tasks: dict[int, asyncio.Task] = {}


_forward_batch_waiters: dict[int, list[asyncio.Future]] = {}


_forward_batch_deadlines: dict[int, float] = {}


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
    html = _strip_visible_urls(html, html=True)
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
    html = _strip_visible_urls(html, html=True)

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
