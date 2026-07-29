"""Retrieve English YouTube transcripts through the RapidAPI transcript service."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_RAPID_HOST = "youtube-transcript3.p.rapidapi.com"
_TRANSCRIPT_URL = f"https://{_RAPID_HOST}/api/transcript-with-url"
_YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_THUMBNAIL_PREFERENCE = ("maxres", "standard", "high", "medium", "default")


class YouTubeImportError(Exception):
    """An expected, user-facing failure while importing a YouTube transcript."""


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    thumbnail_url: str


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
    """Fetch a public video's title and best available thumbnail from YouTube."""
    if not youtube_api_key:
        raise YouTubeImportError("برای دریافت عنوان و تصویر ویدیو، YOUTUBE_API_KEY را تنظیم کنید.")
    try:
        response = requests.get(
            _YOUTUBE_VIDEOS_URL,
            params={
                "part": "snippet",
                "id": extract_video_id(url),
                "key": youtube_api_key,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise YouTubeImportError("دریافت اطلاعات ویدیو از YouTube ناموفق بود.") from exc

    items = data.get("items") if isinstance(data, dict) else None
    snippet = items[0].get("snippet") if isinstance(items, list) and items else None
    if not isinstance(snippet, dict):
        raise YouTubeImportError("اطلاعات عمومی این ویدیوی YouTube پیدا نشد.")

    title = str(snippet.get("title") or "").strip()
    thumbnails = snippet.get("thumbnails") or {}
    thumbnail_url = next(
        (
            str(thumbnails[size].get("url"))
            for size in _THUMBNAIL_PREFERENCE
            if isinstance(thumbnails.get(size), dict) and thumbnails[size].get("url")
        ),
        "",
    )
    if not title or not thumbnail_url:
        raise YouTubeImportError("عنوان یا تصویر این ویدیوی YouTube در دسترس نیست.")
    return VideoMetadata(title=title, thumbnail_url=thumbnail_url)


def _clean_lines(lines: list[str]) -> str:
    result = []
    previous = ""
    for line in lines:
        line = re.sub(r"\s+", " ", html.unescape(line)).strip()
        if line and line.casefold() != previous.casefold():
            result.append(line)
            previous = line
    return "\n".join(result)


def fetch_english_transcript(url: str, rapidapi_key: str | None) -> str:
    """Fetch a flattened English transcript through RapidAPI."""
    extract_video_id(url)  # Validate the input before sending it to the provider.
    if not rapidapi_key:
        raise YouTubeImportError("برای دریافت زیرنویس، X_RAPIDAPI_KEY را تنظیم کنید.")
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
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise YouTubeImportError("دریافت زیرنویس از سرویس RapidAPI ناموفق بود.") from exc
    if not isinstance(data, dict) or not data.get("success"):
        raise YouTubeImportError("سرویس RapidAPI برای این ویدیو زیرنویس انگلیسی پیدا نکرد.")
    transcript = data.get("transcript")
    if not isinstance(transcript, str):
        raise YouTubeImportError("سرویس RapidAPI پاسخ زیرنویس قابل استفاده‌ای نداد.")
    text = _clean_lines(transcript.splitlines())
    if len(text) < 40:
        raise YouTubeImportError("زیرنویس انگلیسی قابل استفاده‌ای برای این ویدیو پیدا نشد.")
    return text
