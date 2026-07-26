"""Read public X posts and their media through the official X API."""
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import requests


API_BASE = "https://api.x.com/2"
_POST_ID_RE = re.compile(r"(?:x\.com|twitter\.com)/[^/]+/status/(\d+)", re.IGNORECASE)


class XPostError(Exception):
    pass


@dataclass
class Media:
    url: str
    kind: str


@dataclass
class Post:
    id: str
    text: str
    author: str
    media: list[Media] = field(default_factory=list)


def extract_post_id(url: str) -> str:
    match = _POST_ID_RE.search(url.strip())
    if not match:
        raise XPostError("Please send a public x.com/.../status/... link.")
    return match.group(1)


def _request(path: str, token: str, params: dict | None = None) -> dict:
    response = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=25,
    )
    if response.status_code in {401, 403}:
        raise XPostError("X rejected the Bearer token or this API plan does not permit the request.")
    if response.status_code == 404:
        raise XPostError("That X post is unavailable, deleted, private, or restricted.")
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise XPostError(f"X API request failed: {exc}") from exc
    return response.json()


def _media_from_includes(data: dict) -> dict[str, Media]:
    result = {}
    for media in data.get("includes", {}).get("media", []):
        url = media.get("url") or media.get("preview_image_url")
        variants = media.get("variants", [])
        mp4 = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
        if mp4:
            url = max(mp4, key=lambda v: v.get("bit_rate", 0))["url"]
        if url:
            result[media["media_key"]] = Media(url=url, kind=media.get("type", "photo"))
    return result


def _to_post(tweet: dict, includes: dict) -> Post:
    users = {u["id"]: u.get("username", "X") for u in includes.get("users", [])}
    media_by_key = _media_from_includes({"includes": includes})
    keys = tweet.get("attachments", {}).get("media_keys", [])
    return Post(
        id=tweet["id"],
        text=tweet.get("text", "").strip(),
        author=users.get(tweet.get("author_id"), "X"),
        media=[media_by_key[key] for key in keys if key in media_by_key],
    )


def fetch_post_and_thread(url: str, token: str) -> list[Post]:
    """Fetch a post and, when the API tier permits it, its recent thread replies."""
    post_id = extract_post_id(url)
    params = {
        "expansions": "author_id,attachments.media_keys",
        "tweet.fields": "conversation_id,created_at",
        "user.fields": "username",
        "media.fields": "url,preview_image_url,variants,type",
    }
    data = _request(f"/tweets/{post_id}", token, params)
    root = data.get("data")
    if not root:
        raise XPostError("X returned no post data.")
    posts = [_to_post(root, data.get("includes", {}))]

    conversation_id = root.get("conversation_id")
    author = posts[0].author
    if not conversation_id or not author:
        return posts

    # Recent search is deliberately best-effort: some X plans do not include it,
    # and older threads may be outside its retention window.
    try:
        thread_data = _request(
            "/tweets/search/recent",
            token,
            {
                **params,
                "query": f"conversation_id:{conversation_id} from:{author}",
                "max_results": 100,
            },
        )
    except XPostError:
        return posts

    thread = [_to_post(tweet, thread_data.get("includes", {})) for tweet in thread_data.get("data", [])]
    if not thread:
        return posts
    by_id = {post.id: post for post in thread}
    by_id.setdefault(posts[0].id, posts[0])
    # Search results have timestamps only when requested. Stable ID ordering is a
    # reasonable fallback for a single author thread.
    return sorted(by_id.values(), key=lambda post: int(post.id))
