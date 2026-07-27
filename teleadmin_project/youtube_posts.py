"""Fetch YouTube media and captions through the RapidAPI downloader service."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

import requests


_HOST = "youtube-media-downloader.p.rapidapi.com"
_DETAILS_URL = f"https://{_HOST}/v2/video/details"
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class YouTubeImportError(Exception):
    """An expected, user-facing failure while importing a YouTube video."""


@dataclass(frozen=True)
class VideoDetails:
    title: str
    video_url: str
    height: int
    subtitle_url: str | None


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


def _headers(key: str) -> dict[str, str]:
    return {
        "x-rapidapi-host": _HOST,
        "x-rapidapi-key": key,
        "Content-Type": "application/json",
    }


def _request_details(video_id: str, key: str) -> dict:
    try:
        response = requests.get(
            _DETAILS_URL,
            params={"videoId": video_id, "urlAccess": "normal", "videos": "auto", "audios": "auto"},
            headers=_headers(key),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise YouTubeImportError("دریافت اطلاعات ویدیو از سرویس YouTube ناموفق بود.") from exc
    if not isinstance(data, dict) or data.get("errorId"):
        raise YouTubeImportError("سرویس YouTube نتوانست این ویدیو را آماده کند.")
    return data


def _select_video(items: list) -> dict:
    choices = [
        item for item in items
        if isinstance(item, dict)
        and item.get("url")
        and item.get("hasAudio")
        and (item.get("extension", "").lower() == "mp4" or item.get("mimeType", "").split(";", 1)[0] == "video/mp4")
        and isinstance(item.get("height"), int)
        and item["height"] <= 720
    ]
    if not choices:
        raise YouTubeImportError("نسخهٔ MP4 دارای صدا با کیفیت حداکثر 720p برای این ویدیو موجود نیست.")
    return max(choices, key=lambda item: (item["height"], item.get("width", 0), item.get("size", 0)))


def _select_english_subtitle(items: list) -> str | None:
    choices = [item for item in items if isinstance(item, dict) and item.get("url")]
    # The provider currently exposes only a language code, not a manual/auto flag.
    exact = next((item for item in choices if str(item.get("code", "")).lower() == "en"), None)
    if exact:
        return exact["url"]
    english = next((item for item in choices if str(item.get("code", "")).lower().startswith("en")), None)
    return english["url"] if english else None


def get_video_details(url: str, key: str | None) -> VideoDetails:
    if not key:
        raise YouTubeImportError("برای دریافت ویدیو، X_RAPIDAPI_KEY را تنظیم کنید.")
    data = _request_details(extract_video_id(url), key)
    video = _select_video((data.get("videos") or {}).get("items") or [])
    subtitle_url = _select_english_subtitle((data.get("subtitles") or {}).get("items") or [])
    title = str(data.get("title") or "ویدیو یوتیوب").strip()
    return VideoDetails(title=title, video_url=video["url"], height=video["height"], subtitle_url=subtitle_url)


def download_video(details: VideoDetails, destination: Path) -> Path:
    """Stream the provider's temporary MP4 URL into the caller-owned temp directory."""
    path = destination / "youtube-video.mp4"
    try:
        with requests.get(details.video_url, stream=True, timeout=90) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
    except (OSError, requests.RequestException) as exc:
        path.unlink(missing_ok=True)
        raise YouTubeImportError("دانلود فایل ویدیو از سرویس YouTube ناموفق بود.") from exc
    if not path.is_file() or not path.stat().st_size:
        raise YouTubeImportError("فایل دانلودشدهٔ ویدیو خالی است.")
    return path


def _dedupe(lines: list[str]) -> str:
    result = []
    previous = ""
    for line in lines:
        line = re.sub(r"\s+", " ", html.unescape(line)).strip()
        if line and line.casefold() != previous.casefold():
            result.append(line)
            previous = line
    return "\n".join(result)


def _parse_xml_subtitles(text: str) -> str:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return ""
    return _dedupe(["".join(node.itertext()) for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"text", "p"}])


def parse_subtitles(text: str) -> str:
    """Extract readable text from YouTube XML, WebVTT, or SRT caption files."""
    stripped = text.lstrip("\ufeff").strip()
    if stripped.startswith("<"):
        parsed = _parse_xml_subtitles(stripped)
        if parsed:
            return parsed
    lines = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or line.isdigit() or " --> " in line:
            continue
        line = re.sub(r"<[^>]+>", "", line)
        lines.append(line)
    return _dedupe(lines)


def download_transcript(subtitle_url: str) -> str:
    try:
        response = requests.get(subtitle_url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise YouTubeImportError("دریافت زیرنویس ویدیو ناموفق بود.") from exc
    transcript = parse_subtitles(response.text)
    if len(transcript) < 40:
        raise YouTubeImportError("زیرنویس انگلیسی قابل استفاده‌ای برای این ویدیو پیدا نشد.")
    return transcript
