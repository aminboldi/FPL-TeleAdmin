"""Poll monitored YouTube channels and hand new non-live uploads to the importer."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

import runtime_config
import youtube_posts


logger = logging.getLogger(__name__)

_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{20,}$")
_POLL_SECONDS = 600
_RECENT_UPLOADS = 15


class YouTubeMonitorError(Exception):
    """A user-facing configuration error for monitored channels."""


@dataclass(frozen=True)
class MonitoredVideo:
    id: str
    channel_id: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


def _api_get(url: str, *, params: dict) -> dict:
    try:
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise YouTubeMonitorError("دریافت اطلاعات از YouTube ناموفق بود.") from exc
    if not isinstance(data, dict):
        raise YouTubeMonitorError("پاسخ YouTube قابل استفاده نیست.")
    return data


def _channel_filter(value: str) -> dict[str, str]:
    value = value.strip()
    if _CHANNEL_ID_RE.fullmatch(value):
        return {"id": value}

    parsed = urlparse(value if "://" in value else f"https://youtube.com/{value.lstrip('/')}")
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if len(parts) >= 2 and parts[0] == "channel" and _CHANNEL_ID_RE.fullmatch(parts[1]):
            return {"id": parts[1]}
        if parts and parts[0].startswith("@"):
            return {"forHandle": parts[0]}
    if value.startswith("@") and len(value) > 1:
        return {"forHandle": value}
    raise YouTubeMonitorError(
        "لینک کانال، شناسهٔ UC… یا هندل @channel را ارسال کنید."
    )


def resolve_channel(value: str, api_key: str | None) -> dict:
    """Resolve a public channel URL, handle, or UC ID to its uploads playlist."""
    if not api_key:
        raise YouTubeMonitorError("برای پایش کانال‌ها، YOUTUBE_API_KEY را تنظیم کنید.")
    data = _api_get(
        _CHANNELS_URL,
        params={
            "part": "snippet,contentDetails",
            "key": api_key,
            **_channel_filter(value),
        },
    )
    items = data.get("items")
    channel = items[0] if isinstance(items, list) and items else None
    if not isinstance(channel, dict):
        raise YouTubeMonitorError("این کانال YouTube پیدا نشد.")
    uploads = (channel.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")
    title = (channel.get("snippet") or {}).get("title")
    channel_id = channel.get("id")
    if not all(isinstance(value, str) and value.strip() for value in (uploads, title, channel_id)):
        raise YouTubeMonitorError("فهرست ویدیوهای این کانال در دسترس نیست.")
    return {"id": channel_id, "title": title.strip(), "uploads_playlist_id": uploads.strip()}


def _recent_upload_ids(playlist_id: str, api_key: str) -> list[str]:
    data = _api_get(
        _PLAYLIST_ITEMS_URL,
        params={
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": _RECENT_UPLOADS,
            "key": api_key,
        },
    )
    return [
        item.get("contentDetails", {}).get("videoId")
        for item in data.get("items", [])
        if isinstance(item, dict)
        and isinstance(item.get("contentDetails", {}).get("videoId"), str)
    ]


def _videos(video_ids: list[str], api_key: str) -> dict[str, dict]:
    if not video_ids:
        return {}
    data = _api_get(
        _VIDEOS_URL,
        params={
            "part": "snippet,status,contentDetails,player,liveStreamingDetails",
            "id": ",".join(video_ids),
            "key": api_key,
            "maxWidth": 640,
            "maxHeight": 640,
        },
    )
    return {
        item["id"]: item
        for item in data.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _is_publishable_video(video: dict) -> bool:
    snippet = video.get("snippet") or {}
    status = video.get("status") or {}
    # `liveStreamingDetails` is present for scheduled, active, and archived
    # broadcasts. The extra snippet check covers a broadcast before details are
    # populated.
    return (
        status.get("privacyStatus") == "public"
        and not video.get("liveStreamingDetails")
        and snippet.get("liveBroadcastContent", "none") == "none"
    )


def subscribe(value: str, api_key: str | None, actor_id: int) -> str:
    """Add a channel and mark its current uploads as the no-post baseline."""
    channel = resolve_channel(value, api_key)
    assert api_key  # checked by resolve_channel
    # Fetch the baseline before persisting the subscription. If YouTube is
    # temporarily unavailable, the channel is not left enabled without a
    # baseline that could cause historical uploads to be posted later.
    current_video_ids = _recent_upload_ids(channel["uploads_playlist_id"], api_key)
    if not runtime_config.add_youtube_channel(
        channel["id"], channel["title"], channel["uploads_playlist_id"], actor_id
    ):
        return f"<b>{channel['title']}</b> از قبل در فهرست پایش است."
    for video_id in current_video_ids:
        runtime_config.mark_youtube_video(video_id, channel["id"], "baseline")
    return f"✅ کانال <b>{channel['title']}</b> به فهرست پایش اضافه شد. ویدیوهای فعلی منتشر نمی‌شوند."


def unsubscribe(value: str, api_key: str | None, actor_id: int) -> str:
    channel = resolve_channel(value, api_key)
    title = runtime_config.remove_youtube_channel(channel["id"], actor_id)
    if not title:
        return "این کانال در فهرست پایش نیست."
    return f"✅ کانال <b>{title}</b> از فهرست پایش حذف شد."


def list_channels() -> str:
    channels = runtime_config.youtube_channels()
    if not channels:
        return "هیچ کانال YouTube برای پایش ثبت نشده است."
    rows = [
        f"<blockquote><b>{channel['title']}</b>\n<code>{channel['channel_id']}</code></blockquote>"
        for channel in channels
    ]
    return "<b>📺 کانال‌های YouTube تحت پایش</b>\n\n" + "\n".join(rows)


def find_new_videos(api_key: str | None) -> list[MonitoredVideo]:
    """Return unseen, public, non-live uploads and remember excluded videos."""
    if not api_key:
        return []
    candidates: list[MonitoredVideo] = []
    for channel in runtime_config.youtube_channels():
        video_ids = _recent_upload_ids(channel["uploads_playlist_id"], api_key)
        unseen_ids = [video_id for video_id in video_ids if not runtime_config.youtube_video_seen(video_id)]
        videos = _videos(unseen_ids, api_key)
        for video_id in unseen_ids:
            video = videos.get(video_id)
            if not video or not _is_publishable_video(video):
                runtime_config.mark_youtube_video(video_id, channel["channel_id"], "skipped")
            elif youtube_posts.is_ad_title((video.get("snippet") or {}).get("title", "")):
                runtime_config.mark_youtube_video(video_id, channel["channel_id"], "skipped")
                logger.info("Skipping YouTube ad video %s", video_id)
            elif youtube_posts.is_short_video(video):
                runtime_config.mark_youtube_video(video_id, channel["channel_id"], "skipped")
                logger.info("Skipping YouTube Short %s", video_id)
            else:
                candidates.append(MonitoredVideo(video_id, channel["channel_id"]))
    return candidates


async def run_monitor(
    api_key: str | None,
    import_video: Callable[[str], Awaitable[str]],
    on_error: Callable[[MonitoredVideo, Exception], Awaitable[None]] | None = None,
) -> None:
    """Continuously queue newly uploaded non-live videos through the /y pipeline."""
    if not api_key:
        logger.warning("YouTube monitoring disabled: YOUTUBE_API_KEY is not configured")
        return
    logger.info("YouTube channel monitor started (polling every %d minutes)", _POLL_SECONDS // 60)
    await asyncio.sleep(10)
    while True:
        try:
            for video in await asyncio.to_thread(find_new_videos, api_key):
                try:
                    await import_video(video.url)
                except Exception as exc:
                    # Captions are often published shortly after the video.
                    # Leave it unseen so the next poll can retry rather than
                    # permanently dropping a valid upload.
                    logger.warning("Could not import monitored YouTube video %s yet: %s", video.id, exc)
                    if on_error:
                        try:
                            await on_error(video, exc)
                        except Exception:
                            logger.exception("Could not report YouTube import failure for %s", video.id)
                else:
                    runtime_config.mark_youtube_video(video.id, video.channel_id, "posted")
                    logger.info("Queued monitored YouTube video %s", video.id)
        except Exception as exc:
            logger.error("YouTube monitor error: %s", exc)
        await asyncio.sleep(_POLL_SECONDS)
