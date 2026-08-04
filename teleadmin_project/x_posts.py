"""Read public X posts and their media through the official X API."""
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import requests


API_BASE = "https://api.x.com/2"
_POST_ID_RE = re.compile(r"(?:x\.com|twitter\.com)/[^/]+/status/(\d+)", re.IGNORECASE)
_POST_URL_RE = re.compile(r"(?:x\.com|twitter\.com)/([^/?#]+)/status/(\d+)", re.IGNORECASE)
_RAPID_HOST = "x-com2.p.rapidapi.com"


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


def _tweet_text(tweet: dict) -> str:
    """Return the expanded text when X stores a long post as a Note Tweet."""
    note_tweet = tweet.get("note_tweet") or {}
    result = note_tweet.get("note_tweet_results", {}).get("result", {})
    text = result.get("text")
    if text:
        return text.strip()
    # Some RapidAPI response variants put the result one level higher.
    result = tweet.get("note_tweet_results", {}).get("result", {})
    text = result.get("text")
    if text:
        return text.strip()
    legacy = tweet.get("legacy", {})
    return (legacy.get("full_text") or tweet.get("text") or "").strip()


def extract_post_id(url: str) -> str:
    match = _POST_ID_RE.search(url.strip())
    if not match:
        raise XPostError("Please send a public x.com/.../status/... link.")
    return match.group(1)


def _extract_username(url: str) -> str:
    match = _POST_URL_RE.search(url.strip())
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
        text=_tweet_text(tweet),
        author=users.get(tweet.get("author_id"), "X"),
        media=[media_by_key[key] for key in keys if key in media_by_key],
    )


def _rapid_request(path: str, key: str, params: dict) -> dict:
    response = requests.get(
        f"https://{_RAPID_HOST}{path}", params=params,
        headers={"x-rapidapi-host": _RAPID_HOST, "x-rapidapi-key": key}, timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise XPostError(f"RapidAPI X request failed: {exc}") from exc
    return response.json()


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _rapid_post(tweet: dict, author: str) -> Post:
    legacy = tweet.get("legacy", {})
    media_rows = legacy.get("extended_entities", legacy.get("entities", {})).get("media", [])
    media = []
    for item in media_rows:
        url = item.get("media_url_https")
        kind = item.get("type", "photo")
        variants = item.get("video_info", {}).get("variants", [])
        mp4 = [variant for variant in variants if variant.get("content_type") == "video/mp4" and variant.get("url")]
        if mp4:
            url = max(mp4, key=lambda variant: variant.get("bitrate", 0))["url"]
        if url:
            media.append(Media(url=url, kind=kind))
    text = _tweet_text(tweet)
    # The source appends a t.co URL for attached media; Telegram gets the media
    # separately, so remove that redundant tail from the translated caption.
    text = re.sub(r"\s+https://t\.co/\S+$", "", text)
    return Post(id=tweet["rest_id"], text=text, author=author, media=media)


def _rapid_reply_parent_id(tweet: dict) -> str:
    return str((tweet.get("legacy") or {}).get("in_reply_to_status_id_str") or "")


def _fetch_rapid_post_and_thread(url: str, key: str) -> list[Post]:
    post_id = extract_post_id(url)
    timeline = _rapid_request("/TweetDetail/", key, {"tweetId": post_id})
    tweets = {}
    for row in _walk(timeline):
        tweet_id = row.get("rest_id")
        legacy = row.get("legacy")
        if not tweet_id or not isinstance(legacy, dict) or not _tweet_text(row):
            continue
        previous = tweets.get(tweet_id)
        # GraphQL responses contain duplicate Tweet objects. Keep the version
        # with the richest media payload rather than a later shallow wrapper.
        media_count = len(legacy.get("extended_entities", {}).get("media", []))
        previous_count = len((previous or {}).get("legacy", {}).get("extended_entities", {}).get("media", []))
        if previous is None or media_count > previous_count:
            tweets[tweet_id] = row
    def tweet_author(tweet: dict) -> str:
        return (
            tweet.get("core", {}).get("user_results", {}).get("result", {})
            .get("legacy", {}).get("screen_name", "")
        )

    root = tweets.get(post_id)
    if not root:
        raise XPostError("RapidAPI returned no readable post for this link.")
    author = tweet_author(root) or _extract_username(url)
    # Conversation + author is not enough: an author can reply to someone else's
    # comment inside the same conversation. Follow only replies whose parent is
    # the submitted post or an already accepted self-authored thread post.
    thread_tweets = {post_id: root}
    pending = [tweet for tweet_id, tweet in tweets.items() if tweet_id != post_id]
    while pending:
        accepted = []
        for tweet in pending:
            if tweet_author(tweet).lower() != author.lower():
                continue
            if _rapid_reply_parent_id(tweet) in thread_tweets:
                thread_tweets[tweet["rest_id"]] = tweet
                accepted.append(tweet)
        if not accepted:
            break
        accepted_ids = {tweet["rest_id"] for tweet in accepted}
        pending = [tweet for tweet in pending if tweet["rest_id"] not in accepted_ids]

    thread = [_rapid_post(tweet, author) for tweet in thread_tweets.values()]
    if not thread:
        raise XPostError("RapidAPI returned the post but no publishable thread content.")
    return sorted(thread, key=lambda post: int(post.id))


def fetch_post_and_thread(url: str, token: str | None = None, rapidapi_key: str | None = None) -> list[Post]:
    """Fetch a post and, when the API tier permits it, its recent thread replies."""
    if rapidapi_key:
        return _fetch_rapid_post_and_thread(url, rapidapi_key)
    if not token:
        raise XPostError("No X API credential is configured.")
    post_id = extract_post_id(url)
    params = {
        "expansions": "author_id,attachments.media_keys",
        "tweet.fields": "conversation_id,created_at,note_tweet,referenced_tweets",
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

    raw_thread = {
        str(tweet["id"]): tweet
        for tweet in thread_data.get("data", [])
        if tweet.get("author_id") == root.get("author_id")
    }
    raw_thread[root["id"]] = root
    accepted = {root["id"]: root}
    pending = [tweet for tweet_id, tweet in raw_thread.items() if tweet_id != root["id"]]
    while pending:
        newly_accepted = []
        for tweet in pending:
            parent_id = next(
                (
                    str(reference.get("id"))
                    for reference in tweet.get("referenced_tweets", [])
                    if reference.get("type") == "replied_to"
                ),
                "",
            )
            if parent_id in accepted:
                accepted[tweet["id"]] = tweet
                newly_accepted.append(tweet)
        if not newly_accepted:
            break
        accepted_ids = {tweet["id"] for tweet in newly_accepted}
        pending = [tweet for tweet in pending if tweet["id"] not in accepted_ids]

    thread = [_to_post(tweet, thread_data.get("includes", {})) for tweet in accepted.values()]
    if not thread:
        return posts
    # Search results have timestamps only when requested. Stable ID ordering is a
    # reasonable fallback for a single author thread.
    return sorted(thread, key=lambda post: int(post.id))
