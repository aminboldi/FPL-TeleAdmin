"""Short-lived, token-protected web editor for owned Telegraph pages."""
import asyncio
import html
import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup
from telegraph.utils import ALLOWED_TAGS, html_to_nodes

import articles
import article_catalog

logger = logging.getLogger(__name__)

_EDITOR_PATH = "/telegraph/edit/"
_LINK_LIFETIME_SECONDS = 15 * 60
_OPEN_EDITOR_LIFETIME_SECONDS = 2 * 60 * 60
_MAX_REQUEST_BYTES = 256 * 1024
_CATALOG_PAGE_SIZE = 24
_catalog_sync_lock = asyncio.Lock()
_catalog_synced = False


@dataclass
class _EditGrant:
    page_url: str
    expires_at: float
    opened: bool = False


_grants: dict[str, _EditGrant] = {}


def _public_base_url() -> str:
    """Resolve the public app URL, preferring an explicit override."""
    raw = (
        os.getenv("TELEGRAPH_EDITOR_BASE_URL")
        or os.getenv("COOLIFY_URL")
        or os.getenv("COOLIFY_FQDN")
        or ""
    ).split(",", 1)[0].strip().rstrip("/")
    if not raw:
        raw = f"http://127.0.0.1:{os.getenv('PORT', '8080')}"
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("TELEGRAPH_EDITOR_BASE_URL is not a valid HTTP(S) URL.")
    return raw


def create_edit_url(page_url: str) -> str:
    """Issue a short-lived capability URL for one selected page."""
    now = time.time()
    for token, grant in list(_grants.items()):
        if grant.expires_at <= now:
            _grants.pop(token, None)
    token = secrets.token_urlsafe(32)
    _grants[token] = _EditGrant(page_url=page_url, expires_at=now + _LINK_LIFETIME_SECONDS)
    return f"{_public_base_url()}{_EDITOR_PATH}{token}"


def public_catalog_url() -> str:
    """Return the public root URL used for the article catalog."""
    return _public_base_url() + "/"


def _grant_for(token: str, *, open_editor: bool = False) -> _EditGrant | None:
    grant = _grants.get(token)
    if not grant or grant.expires_at <= time.time():
        _grants.pop(token, None)
        return None
    if open_editor and not grant.opened:
        grant.opened = True
        grant.expires_at = time.time() + _OPEN_EDITOR_LIFETIME_SECONDS
    return grant


def _clean_editor_html(value: str) -> str:
    """Normalize contenteditable output to tags accepted by Telegraph."""
    soup = BeautifulSoup(value, "html.parser")
    for tag in list(soup.find_all(True)):
        name = tag.name.lower()
        if name in {"script", "style", "form", "input", "button"}:
            tag.decompose()
            continue
        if name == "div":
            tag.name = "p"
            name = "p"
        elif name in {"h1", "h2", "h5", "h6"}:
            tag.name = "h3"
            name = "h3"
        elif name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        allowed_attrs = {"href"} if name == "a" else {"src"} if name in {
            "img", "iframe", "video",
        } else set()
        for attr in list(tag.attrs):
            if attr not in allowed_attrs:
                del tag.attrs[attr]
        for attr in allowed_attrs.intersection(tag.attrs):
            scheme = urlparse(str(tag.attrs[attr])).scheme.lower()
            allowed_schemes = {"", "http", "https"}
            if attr == "href":
                allowed_schemes.update({"mailto", "tg"})
            if scheme not in allowed_schemes:
                del tag.attrs[attr]
    cleaned = soup.decode_contents().strip()
    # Validate before the API request so malformed/unsupported HTML gets a useful error.
    html_to_nodes(cleaned)
    if (
        not BeautifulSoup(cleaned, "html.parser").get_text(strip=True)
        and not any(marker in cleaned for marker in ("<img", "<iframe", "<video"))
    ):
        raise ValueError("The article body cannot be empty.")
    return cleaned


def _page_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; background: #f4f5f7; color: #202124; }}
    main {{ max-width: 820px; margin: 24px auto; padding: 0 16px 40px; }}
    .card {{ background: white; border-radius: 14px; padding: 18px; box-shadow: 0 2px 12px #0001; }}
    h1 {{ font-size: 20px; margin: 0 0 16px; }}
    label {{ display: block; font-weight: 700; margin: 14px 0 6px; }}
    input[type=text] {{ width: 100%; box-sizing: border-box; padding: 11px; border: 1px solid #ccd0d5; border-radius: 8px; font: inherit; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; direction: ltr; }}
    .toolbar button {{ min-width: 38px; padding: 7px 10px; border: 1px solid #ccd0d5; border-radius: 7px; background: #fff; cursor: pointer; }}
    #editor {{ min-height: 420px; border: 1px solid #ccd0d5; border-radius: 8px; padding: 16px; outline: none; line-height: 1.8; }}
    #editor:focus {{ border-color: #168acd; box-shadow: 0 0 0 2px #168acd22; }}
    #editor img {{ max-width: 100%; height: auto; }}
    .save {{ margin-top: 16px; width: 100%; border: 0; border-radius: 9px; padding: 12px; background: #168acd; color: #fff; font: inherit; font-weight: 700; cursor: pointer; }}
    .note {{ color: #646a73; font-size: 13px; }}
    .error {{ color: #a82121; background: #fff0f0; border-radius: 8px; padding: 12px; }}
    .success {{ color: #116329; background: #effaf1; border-radius: 8px; padding: 12px; }}
    a {{ color: #168acd; }}
  </style>
</head>
<body><main><div class="card">{body}</div></main></body>
</html>"""


def _editor_page(page: dict) -> str:
    title = str(page.get("title") or "")
    content = _clean_editor_html(str(page.get("content") or ""))
    body = f"""
<h1>ویرایش مقالهٔ Telegraph</h1>
<form method="post" id="edit-form">
  <label for="title">عنوان</label>
  <input id="title" name="title" type="text" maxlength="256" required value="{html.escape(title, quote=True)}">
  <label>متن مقاله</label>
  <div class="toolbar">
    <button type="button" data-cmd="bold"><b>B</b></button>
    <button type="button" data-cmd="italic"><i>I</i></button>
    <button type="button" data-block="h3">H3</button>
    <button type="button" data-block="h4">H4</button>
    <button type="button" data-block="p">P</button>
    <button type="button" data-cmd="insertUnorderedList">• List</button>
    <button type="button" id="link-button">Link</button>
  </div>
  <div id="editor" contenteditable="true">{content}</div>
  <textarea id="content" name="content" hidden></textarea>
  <p class="note">ویرایشگر تا دو ساعت باز می‌ماند. ذخیره، همین آدرس مقاله را به‌روزرسانی می‌کند.</p>
  <button class="save" type="submit">ذخیره در Telegraph</button>
</form>
<script>
const editor = document.getElementById('editor');
document.querySelectorAll('[data-cmd]').forEach(b => b.addEventListener('mousedown', e => {{
  e.preventDefault(); document.execCommand(b.dataset.cmd, false, null);
}}));
document.querySelectorAll('[data-block]').forEach(b => b.addEventListener('mousedown', e => {{
  e.preventDefault(); document.execCommand('formatBlock', false, b.dataset.block);
}}));
document.getElementById('link-button').addEventListener('mousedown', e => {{
  e.preventDefault(); const url = prompt('آدرس لینک:');
  if (url) document.execCommand('createLink', false, url);
}});
document.getElementById('edit-form').addEventListener('submit', e => {{
  document.getElementById('content').value = editor.innerHTML;
  if (!confirm('تغییرات در مقالهٔ فعلی ذخیره شود؟')) e.preventDefault();
}});
</script>"""
    return _page_shell("ویرایش مقاله", body)


def _message_page(message: str, *, success: bool = False, article_url: str = "") -> str:
    css_class = "success" if success else "error"
    link = (
        f'<p><a href="{html.escape(article_url, quote=True)}">مشاهدهٔ مقالهٔ به‌روزشده</a></p>'
        if article_url else ""
    )
    return _page_shell(
        "Telegraph",
        f'<div class="{css_class}">{html.escape(message)}</div>{link}',
    )


def _catalog_page(
    pages: list[dict],
    source_tags: list[str],
    *,
    query: str = "",
    source_tag: str = "",
    page_number: int = 1,
    has_next: bool = False,
) -> str:
    """Render the public root-domain Telegraph article catalog."""
    cards = []
    for page in pages:
        title = html.escape(str(page.get("title") or "بدون عنوان"))
        url = html.escape(str(page.get("url") or ""), quote=True)
        summary = html.escape(str(page.get("summary") or ""))
        source = html.escape(str(page.get("source_tag") or "مقاله"))
        image_url = str(page.get("image_url") or "")
        image = ""
        if urlparse(image_url).scheme in {"http", "https"}:
            image = (
                f'<img class="thumb" src="{html.escape(image_url, quote=True)}" '
                f'alt="{source}">'
            )
        published = html.escape(str(page.get("published_at") or "")[:10])
        cards.append(
            f"""
            <article class="card">
              {image}
              <div class="card-body">
                <div class="meta"><span class="tag">{source}</span><span>{published}</span></div>
                <h2><a href="{url}" target="_blank" rel="noopener">{title}</a></h2>
                <p>{summary or "برای خواندن متن کامل، روی عنوان مقاله بزنید."}</p>
                <a class="read" href="{url}" target="_blank" rel="noopener">خواندن مقاله ←</a>
              </div>
            </article>
            """
        )
    cards_html = "".join(cards) or (
        '<div class="empty">مقاله‌ای با این فیلتر پیدا نشد.</div>'
    )

    def filter_url(page_value: int) -> str:
        params = {"page": str(page_value)}
        if query:
            params["q"] = query
        if source_tag:
            params["source"] = source_tag
        return "/?" + urlencode(params)

    tags = ['<option value="">همهٔ منابع</option>']
    tags.extend(
        f'<option value="{html.escape(tag, quote=True)}"'
        f'{" selected" if tag == source_tag else ""}>{html.escape(tag)}</option>'
        for tag in source_tags
    )
    previous = (
        f'<a class="pager" href="{html.escape(filter_url(page_number - 1), quote=True)}">← جدیدتر</a>'
        if page_number > 1 else ""
    )
    next_page = (
        f'<a class="pager" href="{html.escape(filter_url(page_number + 1), quote=True)}">قدیمی‌تر →</a>'
        if has_next else ""
    )
    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>مقالات فارسی فانتزی</title>
  <style>
    :root {{ color-scheme: light; font-family: Tahoma, system-ui, sans-serif; }}
    body {{ margin: 0; background: #f3f5f7; color: #18202a; }}
    header {{ background: #182b3a; color: white; padding: 34px 18px 28px; }}
    .wrap {{ max-width: 1120px; margin: 0 auto; }}
    header h1 {{ margin: 0 0 8px; font-size: 27px; }}
    header p {{ margin: 0; color: #d8e3ea; }}
    form {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 22px auto; }}
    input, select, button {{ border: 1px solid #ccd4dc; border-radius: 8px; padding: 11px 12px; font: inherit; }}
    input {{ flex: 1 1 260px; min-width: 180px; }}
    select {{ min-width: 170px; background: white; }}
    button {{ background: #168acd; border-color: #168acd; color: white; cursor: pointer; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; padding-bottom: 24px; }}
    .card {{ overflow: hidden; background: white; border: 1px solid #e2e7eb; border-radius: 14px; box-shadow: 0 2px 10px #182b3a0d; }}
    .thumb {{ display: block; width: 100%; height: 170px; object-fit: cover; background: #e7edf1; }}
    .card-body {{ padding: 16px; }}
    .meta {{ display: flex; justify-content: space-between; gap: 8px; color: #687582; font-size: 12px; }}
    .tag {{ color: #126a9e; font-weight: 700; }}
    h2 {{ font-size: 19px; line-height: 1.55; margin: 10px 0 8px; }}
    h2 a {{ color: #18202a; text-decoration: none; }}
    .card p {{ color: #53616e; line-height: 1.8; min-height: 52px; margin: 0 0 12px; }}
    .read {{ color: #168acd; font-weight: 700; text-decoration: none; }}
    .pager-row {{ display: flex; justify-content: space-between; min-height: 44px; }}
    .pager {{ color: #168acd; font-weight: 700; text-decoration: none; }}
    .empty {{ background: white; border-radius: 12px; padding: 32px; text-align: center; color: #687582; }}
    @media (max-width: 600px) {{ header h1 {{ font-size: 22px; }} .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header><div class="wrap"><h1>مقالات فارسی فانتزی</h1><p>آرشیو مقالات منتشرشده در Telegraph</p></div></header>
  <main class="wrap">
    <form method="get">
      <input name="q" value="{html.escape(query, quote=True)}" placeholder="جست‌وجو در عنوان و خلاصه">
      <select name="source">{"".join(tags)}</select>
      <button type="submit">جست‌وجو</button>
    </form>
    <section class="grid">{cards_html}</section>
    <nav class="pager-row">{previous}{next_page}</nav>
  </main>
</body>
</html>"""


async def _ensure_catalog_synced() -> None:
    global _catalog_synced
    if _catalog_synced:
        return
    async with _catalog_sync_lock:
        if _catalog_synced:
            return
        try:
            await asyncio.to_thread(article_catalog.sync_from_telegraph)
        except Exception:
            logger.exception("Initial Telegraph catalog sync failed")
        _catalog_synced = True


async def _read_request(reader) -> tuple[str, str, dict[str, str], bytes]:
    request_line = await asyncio.wait_for(reader.readline(), timeout=10)
    if not request_line:
        raise ValueError("Empty request")
    parts = request_line.decode("latin-1").strip().split()
    if len(parts) != 3:
        raise ValueError("Malformed request line")
    method, target, _ = parts
    headers: dict[str, str] = {}
    header_bytes = 0
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if line in {b"\r\n", b"\n", b""}:
            break
        header_bytes += len(line)
        if header_bytes > 32 * 1024:
            raise OverflowError("Request headers are too large")
        key, separator, value = line.decode("latin-1").partition(":")
        if not separator:
            raise ValueError("Malformed header")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length < 0 or length > _MAX_REQUEST_BYTES:
        raise OverflowError("Request body is too large")
    body = await asyncio.wait_for(reader.readexactly(length), timeout=15) if length else b""
    return method.upper(), target, headers, body


async def _send_response(writer, status: int, body: str, *, head: bool = False) -> None:
    reasons = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large", 500: "Internal Server Error"}
    payload = body.encode("utf-8")
    headers = (
        f"HTTP/1.1 {status} {reasons.get(status, '')}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Cache-Control: no-store\r\n"
        "Referrer-Policy: no-referrer\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "X-Frame-Options: DENY\r\n"
        "Content-Security-Policy: default-src 'none'; img-src https: data:; "
        "frame-src https:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(headers + (b"" if head else payload))
    await writer.drain()


async def handle_http(reader, writer) -> None:
    """Serve health checks and the private Telegraph editing endpoint."""
    try:
        try:
            method, target, headers, body = await _read_request(reader)
        except OverflowError:
            await _send_response(writer, 413, "Request too large")
            return
        except Exception:
            await _send_response(writer, 400, "Bad request")
            return

        path = urlparse(target).path
        if path == "/" and method in {"GET", "HEAD"}:
            await _ensure_catalog_synced()
            query = parse_qs(urlparse(target).query)
            search = query.get("q", [""])[0].strip()
            source_tag = query.get("source", [""])[0].strip()
            try:
                page_number = max(1, int(query.get("page", ["1"])[0]))
            except ValueError:
                page_number = 1
            offset = (page_number - 1) * _CATALOG_PAGE_SIZE
            pages = await asyncio.to_thread(
                article_catalog.list_pages,
                query=search,
                source_tag=source_tag,
                limit=_CATALOG_PAGE_SIZE,
                offset=offset,
            )
            has_next = await asyncio.to_thread(
                article_catalog.has_more,
                query=search,
                source_tag=source_tag,
                offset=offset,
                limit=_CATALOG_PAGE_SIZE,
            )
            page = _catalog_page(
                pages,
                await asyncio.to_thread(article_catalog.list_source_tags),
                query=search,
                source_tag=source_tag,
                page_number=page_number,
                has_next=has_next,
            )
            await _send_response(writer, 200, page, head=method == "HEAD")
            return
        if path in {"/health", "/healthz"} and method in {"GET", "HEAD"}:
            await _send_response(writer, 200, "OK", head=method == "HEAD")
            return
        if not path.startswith(_EDITOR_PATH):
            await _send_response(writer, 404, "Not found")
            return
        token = path.removeprefix(_EDITOR_PATH)
        if not token or "/" in token:
            await _send_response(writer, 404, "Not found")
            return
        grant = _grant_for(token, open_editor=method == "GET")
        if not grant:
            await _send_response(writer, 404, _message_page("این لینک ویرایش منقضی یا استفاده شده است."))
            return
        if method == "GET":
            try:
                page = await asyncio.to_thread(articles.get_telegraph_page, grant.page_url)
            except Exception as exc:
                logger.exception("Failed to load Telegraph editor page")
                await _send_response(writer, 500, _message_page(f"دریافت مقاله ناموفق بود: {exc}"))
                return
            await _send_response(writer, 200, _editor_page(page))
            return
        if method != "POST":
            await _send_response(writer, 405, "Method not allowed")
            return
        if not headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            await _send_response(writer, 400, _message_page("فرمت درخواست نامعتبر است."))
            return
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=10)
            title = fields.get("title", [""])[0]
            content = _clean_editor_html(fields.get("content", [""])[0])
            await asyncio.to_thread(articles.edit_telegraph_page, grant.page_url, title, content)
        except Exception as exc:
            logger.exception("Failed to save Telegraph page")
            await _send_response(writer, 400, _message_page(f"ذخیره ناموفق بود: {exc}"))
            return
        _grants.pop(token, None)
        await _send_response(
            writer, 200,
            _message_page("تغییرات با موفقیت ذخیره شد.", success=True, article_url=grant.page_url),
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
