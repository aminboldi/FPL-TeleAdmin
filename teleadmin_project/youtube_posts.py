"""Retrieve English YouTube transcripts through the RapidAPI transcript service."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests

import runtime_config

logger = logging.getLogger(__name__)


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_RAPID_HOST = "youtube-transcript3.p.rapidapi.com"
_TRANSCRIPT_URL = f"https://{_RAPID_HOST}/api/transcript-with-url"
_YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_THUMBNAIL_PREFERENCE = ("maxres", "standard", "high", "medium", "default")
_HASHTAG_RE = re.compile(r"(?<!\w)#[\w-]+", re.UNICODE)
_AD_TITLE_RE = re.compile(r"(?<!\w)#ad(?!\w)|(?<!\w)ad(?!\w)", re.IGNORECASE)
_SHORTS_HASHTAG_RE = re.compile(r"(?<!\w)#shorts(?!\w)", re.IGNORECASE)
_ISO_DURATION_RE = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)
_DESCRIPTION_LINK_RE = re.compile(
    r"(?:https?://|www\.)\S+|\b(?:[\w-]+\.)+(?:com|net|org|io|co|me|tv|gg|fm|ly|be|app|ai)(?:/\S*)?",
    re.IGNORECASE,
)


class YouTubeImportError(Exception):
    """An expected, user-facing failure while importing a YouTube transcript."""


class TranscriptProvidersExhausted(YouTubeImportError):
    """All configured transcript providers failed or had no usable transcript."""


def _duration_seconds(value: str) -> int | None:
    match = _ISO_DURATION_RE.fullmatch(str(value or ""))
    if not match:
        return None
    return (
        int(match.group("hours") or 0) * 3600
        + int(match.group("minutes") or 0) * 60
        + int(match.group("seconds") or 0)
    )


def is_short_video(video: dict, *, source_url: str = "") -> bool:
    """Return whether a public video has YouTube-Shorts characteristics."""
    parsed = urlparse(str(source_url or "").strip())
    if parsed.path.strip("/").split("/", 1)[0].casefold() == "shorts":
        return True

    snippet = video.get("snippet") or {}
    if _SHORTS_HASHTAG_RE.search(
        f"{snippet.get('title', '')}\n{snippet.get('description', '')}"
    ):
        return True

    content_details = video.get("contentDetails") or {}
    duration = _duration_seconds(content_details.get("duration", ""))
    if duration is None or duration > 180:
        return False

    # The API's public player dimensions are available when requested with
    # maxWidth/maxHeight. A square or taller frame is the reliable signal;
    # fileDetails.videoStreams is owner-only and cannot be used here.
    player = video.get("player") or {}
    try:
        width = int(player.get("embedWidth") or 0)
        height = int(player.get("embedHeight") or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width and height:
        return height >= width

    # Some API responses expose this newer aspect-ratio value directly.
    aspect_ratio = str(content_details.get("aspectRatio") or "").upper()
    return aspect_ratio in {"RATIO_1_1", "RATIO_9_16", "1:1", "9:16"}


def is_ad_title(title: str) -> bool:
    """Return whether a video title contains a standalone ad marker."""
    return bool(_AD_TITLE_RE.search(str(title or "")))


def clean_video_title(title: str) -> str:
    """Remove hashtags from a title while keeping the remaining wording."""
    cleaned = _HASHTAG_RE.sub("", str(title or ""))
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = cleaned.strip(" -|–—")
    return cleaned


class _ProviderFailure(Exception):
    """A provider-level failure that should disable the service for this month."""


class _NoTranscript(Exception):
    """The provider responded normally but has no usable transcript for this video."""


_TRANSCRIPT_PROVIDERS = (
    "youtube-captions-transcript-subtitles-video-combiner",
    "youtube-transcripts",
    "youtube-transcript3",
    "youtube-2-transcript",
    "youtube-transcripts-playlists-channels-search1",
)
_PROVIDER_HOSTS = {
    "youtube-captions-transcript-subtitles-video-combiner": (
        "youtube-captions-transcript-subtitles-video-combiner.p.rapidapi.com"
    ),
    "youtube-transcripts": "youtube-transcripts.p.rapidapi.com",
    "youtube-2-transcript": "youtube-2-transcript.p.rapidapi.com",
    "youtube-transcripts-playlists-channels-search1": (
        "youtube-transcripts-playlists-channels-search1.p.rapidapi.com"
    ),
}


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    thumbnail_url: str
    description: str
    channel_title: str


def description_before_first_link_sentence(description: str) -> str:
    """Keep the editorial introduction and remove the link-led promo section.

    YouTube descriptions commonly switch to sponsorship and social links after
    the first sentence containing a URL.  The first such sentence and all text
    after it are deliberately excluded from the channel post.
    """
    match = _DESCRIPTION_LINK_RE.search(description)
    if not match:
        return description.strip()

    # A blank line is also treated as a sentence boundary: creators often put
    # the promotional link on its own line without ending the preceding copy.
    starts = [0]
    starts.extend(
        boundary.end()
        for boundary in re.finditer(r"(?<=[.!?])\s+|\n+", description)
        if boundary.end() <= match.start()
    )
    return description[:starts[-1]].strip()


def extract_video_id(url: str) -> str:
    """Return the ID from a normal YouTube watch/short/share URL."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = parsed.path.strip("/").split("/")
            candidate = parts[1] if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"} else ""
    else:
        candidate = ""
    if not _VIDEO_ID_RE.fullmatch(candidate):
        raise YouTubeImportError("فقط لینک معتبر یک ویدیوی YouTube قابل دریافت است.")
    return candidate


def fetch_video_metadata(url: str, youtube_api_key: str | None) -> VideoMetadata:
    """Fetch a public video's title, channel, description, and thumbnail."""
    if not youtube_api_key:
        raise YouTubeImportError("برای دریافت عنوان و تصویر ویدیو، YOUTUBE_API_KEY را تنظیم کنید.")
    try:
        response = requests.get(
            _YOUTUBE_VIDEOS_URL,
            params={
                "part": "snippet,contentDetails,player",
                "id": extract_video_id(url),
                "key": youtube_api_key,
                "maxWidth": 640,
                "maxHeight": 640,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise YouTubeImportError("دریافت اطلاعات ویدیو از YouTube ناموفق بود.") from exc

    items = data.get("items") if isinstance(data, dict) else None
    video = items[0] if isinstance(items, list) and items else None
    snippet = video.get("snippet") if isinstance(video, dict) else None
    if not isinstance(snippet, dict):
        raise YouTubeImportError("اطلاعات عمومی این ویدیوی YouTube پیدا نشد.")

    raw_title = str(snippet.get("title") or "").strip()
    channel_title = str(snippet.get("channelTitle") or "").strip()
    description = str(snippet.get("description") or "").strip()
    if is_ad_title(raw_title):
        raise YouTubeImportError("این ویدیو به‌دلیل وجود نشانگر تبلیغ در عنوان منتشر نمی‌شود.")
    title = clean_video_title(raw_title)
    thumbnails = snippet.get("thumbnails") or {}
    thumbnail_url = next(
        (
            str(thumbnails[size].get("url"))
            for size in _THUMBNAIL_PREFERENCE
            if isinstance(thumbnails.get(size), dict) and thumbnails[size].get("url")
        ),
        "",
    )
    if not title or not channel_title or not thumbnail_url:
        raise YouTubeImportError("عنوان یا تصویر این ویدیوی YouTube در دسترس نیست.")
    return VideoMetadata(
        title=title,
        thumbnail_url=thumbnail_url,
        description=description,
        channel_title=channel_title,
    )


def _clean_lines(lines: list[str]) -> str:
    result = []
    previous = ""
    for line in lines:
        line = re.sub(r"\s+", " ", html.unescape(line)).strip()
        if re.fullmatch(r"\d{1,6}", line) or re.match(
            r"^\d{2}:\d{2}:\d{2}(?:[,.]\d{3})?\s+-->\s+", line
        ):
            continue
        if line and line.casefold() != previous.casefold():
            result.append(line)
            previous = line
    return "\n".join(result)


def _request_provider_json(
    provider: str,
    method: str,
    path: str,
    rapidapi_key: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict | list | str:
    host = _PROVIDER_HOSTS[provider]
    headers = {
        "x-rapidapi-host": host,
        "x-rapidapi-key": rapidapi_key,
        "Content-Type": "application/json",
    }
    try:
        response = requests.request(
            method,
            f"https://{host}{path}",
            params=params,
            json=json_body,
            headers=headers,
            timeout=45,
        )
    except requests.RequestException as exc:
        raise _ProviderFailure(f"{provider}: network error") from exc
    if response.status_code >= 400:
        if response.status_code in {401, 403, 429}:
            raise _ProviderFailure(f"{provider}: HTTP {response.status_code}")
        raise _ProviderFailure(f"{provider}: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    if not isinstance(payload, (dict, list, str)):
        raise _ProviderFailure(f"{provider}: unsupported response shape")
    return payload


def _extract_transcript_text(payload) -> str:
    """Extract text from the known and common transcript response shapes."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts = []
        for item in payload:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                segment = (
                    item.get("text")
                    or item.get("content")
                    or item.get("caption")
                    or item.get("transcript")
                    or item.get("subtitle")
                    or item.get("subtitles")
                    or item.get("srt")
                    or item.get("answer")
                )
                if isinstance(segment, str):
                    parts.append(segment)
                elif isinstance(segment, (dict, list)):
                    parts.append(_extract_transcript_text(segment))
        return "\n".join(part for part in parts if part)
    if not isinstance(payload, dict):
        return ""
    for key in (
        "transcript", "transcripts", "segments", "captions", "data", "result", "content", "text",
        "answer", "subtitle", "subtitles", "srt", "transcript_text",
    ):
        if key in payload:
            text = _extract_transcript_text(payload[key])
            if text:
                return text
    return ""


def _normalize_provider_transcript(payload) -> str:
    if isinstance(payload, dict) and payload.get("success") is False:
        raise _NoTranscript()
    text = _clean_lines(_extract_transcript_text(payload).splitlines())
    if len(text) < 40:
        raise _NoTranscript()
    return text


def _fetch_main_transcript(url: str, rapidapi_key: str) -> str:
    try:
        response = requests.get(
            _TRANSCRIPT_URL,
            params={"url": url, "flat_text": "true", "lang": "en"},
            headers={
                "x-rapidapi-host": _RAPID_HOST,
                "x-rapidapi-key": rapidapi_key,
                "Content-Type": "application/json",
            },
            timeout=45,
        )
    except requests.RequestException as exc:
        raise _ProviderFailure("youtube-transcript3: network error") from exc
    if response.status_code >= 400:
        raise _ProviderFailure(f"youtube-transcript3: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise _ProviderFailure("youtube-transcript3: invalid JSON response") from exc
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise _NoTranscript()
    return _normalize_provider_transcript(payload)


def _fetch_fallback_transcript(provider: str, url: str, rapidapi_key: str) -> str:
    video_id = extract_video_id(url)
    if provider == "youtube-captions-transcript-subtitles-video-combiner":
        payload = _request_provider_json(
            provider,
            "GET",
            f"/download-all/{video_id}",
            rapidapi_key,
            params={
                "format_subtitle": "srt",
                "format_answer": "json",
                "response_mode": "default",
            },
        )
    elif provider == "youtube-transcripts":
        payload = _request_provider_json(
            provider,
            "GET",
            "/youtube/transcript",
            rapidapi_key,
            params={
                "url": url,
                "videoId": video_id,
                "chunkSize": "500",
                "text": "false",
                "lang": "en",
            },
        )
    elif provider == "youtube-2-transcript":
        payload = _request_provider_json(
            provider,
            "GET",
            "/transcript-with-url",
            rapidapi_key,
            params={"url": url, "flat_text": "false"},
        )
    else:
        payload = _request_provider_json(
            provider,
            "POST",
            "/api/v1/transcripts/video",
            rapidapi_key,
            json_body={"video": video_id},
        )
    return _normalize_provider_transcript(payload)


def fetch_english_transcript(url: str, rapidapi_key: str | None) -> str:
    """Fetch a transcript, disabling failed providers for this month."""
    extract_video_id(url)
    if not rapidapi_key:
        raise YouTubeImportError("برای دریافت زیرنویس، X_RAPIDAPI_KEY را تنظیم کنید.")

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    failures = []
    attempted = []
    for provider in _TRANSCRIPT_PROVIDERS:
        if runtime_config.transcript_provider_is_disabled(provider, month):
            logger.info("Skipping disabled transcript provider %s for %s", provider, month)
            continue
        attempted.append(provider)
        try:
            if provider == "youtube-transcript3":
                text = _fetch_main_transcript(url, rapidapi_key)
            else:
                text = _fetch_fallback_transcript(provider, url, rapidapi_key)
        except _NoTranscript:
            logger.info("Transcript provider %s has no usable captions for this video", provider)
            continue
        except _ProviderFailure as exc:
            failures.append(str(exc))
            runtime_config.disable_transcript_provider(provider, month, str(exc))
            logger.warning("Disabling transcript provider %s until next month: %s", provider, exc)
            continue
        logger.info("Transcript provider %s returned %d characters", provider, len(text))
        return text

    if not attempted:
        raise TranscriptProvidersExhausted(
            "تمام سرویس‌های زیرنویس برای این ماه غیرفعال شده‌اند."
        )
    if failures:
        raise TranscriptProvidersExhausted(
            "همهٔ سرویس‌های زیرنویس در دسترس نبودند؛ وضعیت سهمیهٔ RapidAPI را بررسی کنید."
        )
    raise TranscriptProvidersExhausted(
        "هیچ‌یک از سرویس‌های زیرنویس برای این ویدیو متن قابل استفاده‌ای پیدا نکردند."
    )
