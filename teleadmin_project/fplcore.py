"""Read the public WordPress data used by FPL Core's JavaScript blog."""
import re
from html import unescape
from urllib.parse import quote, unquote, urlparse

import requests
from bs4 import BeautifulSoup

BLOG_URL = "https://www.fplcore.com/blog"
API_URL = "https://public-api.wordpress.com/wp/v2/sites/fplcore.wordpress.com/posts"
# Public author ID in this site's WordPress API. Verify embedded author
# metadata as well, so a missing/mismatched author never becomes eligible.
ARMO_AUTHOR_ID = 284376366
SOURCE_KEY = "fplarmo"
CHANNEL = "@FPL_ARMO"
_HEADERS = {"User-Agent": "TeleAdminArticleMonitor/1.0"}


def _posts(**params) -> list[dict]:
    response = requests.get(
        API_URL, params={"_embed": "true", "status": "publish", **params},
        headers=_HEADERS, timeout=25,
    )
    response.raise_for_status()
    posts = response.json()
    if not isinstance(posts, list):
        raise ValueError("FPL Core returned an invalid article list")
    return posts


def is_armo_post(post: dict) -> bool:
    return post.get("author") == ARMO_AUTHOR_ID and any(
        author.get("id") == ARMO_AUTHOR_ID and (
            str(author.get("slug", "")).casefold() == "fplarmo"
            or str(author.get("name", "")).strip().casefold() == "fplarmo"
        )
        for author in post.get("_embedded", {}).get("author", [])
    )


def post_url(post: dict) -> str:
    slug = str(post.get("slug", ""))
    if not slug or not re.fullmatch(r"[\w%-]+", slug):
        raise ValueError("FPL Core article has an invalid slug")
    return f"{BLOG_URL}/{quote(unquote(slug), safe='-')}"


def plain_text(html: str) -> str:
    return BeautifulSoup(unescape(html), "html.parser").get_text(" ", strip=True)


def discover_posts(limit: int = 12) -> list[dict]:
    # Filtering on the server prevents other authors from crowding Armo's
    # articles out of the first page of results.
    return [
        post for post in _posts(author=ARMO_AUTHOR_ID, per_page=limit, order="desc", orderby="date")
        if post.get("status") == "publish" and is_armo_post(post)
        and not post.get("content", {}).get("protected")
    ]


def is_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"fplcore.com", "www.fplcore.com"}
        and re.fullmatch(r"/blog/[\w%-]+/?", parsed.path) is not None
    )


def fetch_article(url: str) -> dict | None:
    """Fetch full content, including image positions, without reader-mode loss."""
    if not is_article_url(url):
        return None
    slug = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    posts = _posts(slug=slug, per_page=1)
    if not posts:
        return None
    post = posts[0]
    content = post.get("content", {})
    if post.get("status") != "publish" or content.get("protected"):
        return None
    title = plain_text(post.get("title", {}).get("rendered", ""))
    body = content.get("rendered", "")
    if not title or not body.strip():
        return None
    media = post.get("_embedded", {}).get("wp:featuredmedia", [])
    feature = media[0].get("source_url", "") if media else ""
    return {
        "title": title, "html": body, "url": post_url(post),
        "summary": plain_text(post.get("excerpt", {}).get("rendered", "")),
        "date": post.get("date_gmt") or post.get("date", ""),
        "feature_image": feature,
        "source_name": "FPLArmo" if is_armo_post(post) else "FPL Core",
        "content_base_url": post.get("link") or url,
    }
