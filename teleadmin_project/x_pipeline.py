"""X (Twitter) posts submitted through the admin dashboard."""
import asyncio
import os
import re



import x_posts
from app import (
    QUEUED_POST_CONFIRMATION,
    _refresh_translator_model,
    settings,
    translator,
)
from post_format import (
    _build_caption,
    _expand_x_short_links,
    _fix_unclosed_tags,
    _strip_quotes,
    _x_text_to_html,
)
from delivery import (
    _download_remote_media,
    _send_notification,
    _send_to_target,
)


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


def _download_x_media(media: x_posts.Media) -> str:
    return _download_remote_media(media.url, media.kind)


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
