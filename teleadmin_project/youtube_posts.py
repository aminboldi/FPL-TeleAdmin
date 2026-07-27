"""Retrieve English YouTube captions for the transcript-to-article import flow."""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import YouTubeTranscriptApi


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class YouTubeImportError(Exception):
    """An expected, user-facing failure while importing a YouTube transcript."""


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


def _clean_lines(lines: list[str]) -> str:
    result = []
    previous = ""
    for line in lines:
        line = re.sub(r"\s+", " ", html.unescape(line)).strip()
        if line and line.casefold() != previous.casefold():
            result.append(line)
            previous = line
    return "\n".join(result)


def fetch_english_transcript(url: str) -> str:
    """Fetch English manual captions first, then English auto-captions."""
    video_id = extract_video_id(url)
    try:
        tracks = list(YouTubeTranscriptApi().list(video_id))
        english_tracks = [
            track for track in tracks
            if str(getattr(track, "language_code", "")).lower().startswith("en")
        ]
        track = next((item for item in english_tracks if not item.is_generated), None)
        track = track or next((item for item in english_tracks if item.is_generated), None)
        if not track:
            raise YouTubeImportError("زیرنویس انگلیسی برای این ویدیو در دسترس نیست.")
        transcript = track.fetch()
        text = _clean_lines([snippet.text for snippet in transcript])
    except YouTubeImportError:
        raise
    except Exception as exc:
        raise YouTubeImportError("دریافت زیرنویس YouTube ناموفق بود یا توسط YouTube مسدود شد.") from exc
    if len(text) < 40:
        raise YouTubeImportError("زیرنویس انگلیسی قابل استفاده‌ای برای این ویدیو پیدا نشد.")
    return text
