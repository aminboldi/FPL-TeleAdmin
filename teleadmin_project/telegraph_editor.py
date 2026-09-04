"""Short-lived, token-protected web editor for owned Telegraph pages."""
import asyncio
import html
import logging
import os
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup
from telegraph.utils import ALLOWED_TAGS, html_to_nodes

import articles
import article_catalog
import database
import player_names

logger = logging.getLogger(__name__)

_EDITOR_PATH = "/telegraph/edit/"
_ADMIN_PATH = "/telegraph/admin"
_ADMIN_LOGIN_PATH = "/telegraph/admin/login"
_ADMIN_LOGOUT_PATH = "/telegraph/admin/logout"
_ADMIN_HIDE_PATH = "/telegraph/admin/catalog-visibility"
_PLAYERS_PATH = "/players"
_PLAYERS_LOGIN_PATH = "/players/login"
_LINK_LIFETIME_SECONDS = 15 * 60
_OPEN_EDITOR_LIFETIME_SECONDS = 2 * 60 * 60
_ADMIN_SESSION_LIFETIME_SECONDS = 12 * 60 * 60
_MAX_REQUEST_BYTES = 256 * 1024
_CATALOG_PAGE_SIZE = 24
_EDITOR_BLOCK_TAGS = {
    "address", "article", "blockquote", "div", "figure", "h1", "h2",
    "h3", "h4", "h5", "h6", "hr", "ol", "p", "pre", "section", "ul",
}
_STATIC_ASSETS = {
    "/logo.webp": (Path(__file__).resolve().parent.parent / "logo.webp", "image/webp"),
    "/fav-icon.png": (Path(__file__).resolve().parent.parent / "fav-icon.png", "image/png"),
}
_catalog_sync_lock = asyncio.Lock()
_catalog_synced = False


@dataclass
class _EditGrant:
    page_url: str
    expires_at: float
    opened: bool = False


_grants: dict[str, _EditGrant] = {}
_admin_sessions: dict[str, float] = {}


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


def _admin_password() -> str:
    return os.getenv("TELEGRAPH_EDITOR_PASSWORD", "")


def _admin_session_from_headers(headers: dict[str, str]) -> str:
    cookies = SimpleCookie()
    try:
        cookies.load(headers.get("cookie", ""))
    except Exception:
        return ""
    return cookies.get("telegraph_admin").value if cookies.get("telegraph_admin") else ""


def _admin_authenticated(headers: dict[str, str]) -> bool:
    token = _admin_session_from_headers(headers)
    expires_at = _admin_sessions.get(token, 0)
    if not token or expires_at <= time.time():
        _admin_sessions.pop(token, None)
        return False
    return True


def _admin_cookie(token: str, *, max_age: int) -> str:
    secure = "; Secure" if _public_base_url().startswith("https://") else ""
    return (
        f"telegraph_admin={token}; Path=/; Max-Age={max_age}; "
        f"HttpOnly; SameSite=Strict{secure}"
    )


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

    # Some contenteditable/browser combinations can serialize a heading edit
    # as ``<h3>Heading<p>Following paragraph</p></h3>`` even though the
    # editor renders the paragraph separately. Move direct block children out
    # of headings before sending the HTML to Telegraph.
    for heading in list(soup.find_all(("h1", "h2", "h3", "h4", "h5", "h6"))):
        anchor = heading
        for child in list(heading.find_all(recursive=False)):
            if child.name not in _EDITOR_BLOCK_TAGS:
                continue
            child.extract()
            anchor.insert_after(child)
            anchor = child

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


def _page_shell(title: str, body: str, *, page_class: str = "") -> str:
    main_class = f' class="{html.escape(page_class, quote=True)}"' if page_class else ""
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
    input[type=text], input[type=password] {{ width: 100%; box-sizing: border-box; padding: 11px; border: 1px solid #ccd0d5; border-radius: 8px; font: inherit; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; direction: ltr; }}
    .toolbar button {{ min-width: 38px; padding: 7px 10px; border: 1px solid #ccd0d5; border-radius: 7px; background: #fff; cursor: pointer; }}
    #editor {{ min-height: 420px; border: 1px solid #ccd0d5; border-radius: 8px; padding: 16px; outline: none; line-height: 1.8; }}
    #editor:focus {{ border-color: #168acd; box-shadow: 0 0 0 2px #168acd22; }}
    #editor img {{ max-width: 100%; height: auto; }}
    .save {{ margin-top: 16px; width: 100%; border: 0; border-radius: 9px; padding: 12px; background: #168acd; color: #fff; font: inherit; font-weight: 700; cursor: pointer; }}
    .note {{ color: #646a73; font-size: 13px; }}
    .error {{ color: #a82121; background: #fff0f0; border-radius: 8px; padding: 12px; }}
    .success {{ color: #116329; background: #effaf1; border-radius: 8px; padding: 12px; }}
    .admin-list {{ display: grid; gap: 10px; margin-top: 18px; }}
    .admin-row {{ display: flex; align-items: center; justify-content: space-between; gap: 14px; border: 1px solid #e1e5e9; border-radius: 10px; padding: 12px; }}
    .admin-title {{ flex: 1 1 auto; min-width: 0; }}
    .admin-row h2 {{ margin: 0 0 4px; font-size: 16px; overflow-wrap: anywhere; }}
    .admin-row .note {{ overflow-wrap: anywhere; margin: 0; }}
    .admin-row.hidden {{ opacity: .68; }}
    .admin-actions {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; flex: 0 0 auto; }}
    .admin-edit {{ flex: 0 0 auto; background: #310b34; color: #fff; border-radius: 7px; padding: 9px 14px; text-decoration: none; font-weight: 700; }}
    .admin-action {{ margin: 0; }}
    .admin-hide {{ border: 1px solid #ccd0d5; border-radius: 7px; padding: 9px 14px; background: #fff; cursor: pointer; font: inherit; }}
    .admin-action-icon {{ display: none; }}
    .logout {{ margin-top: 18px; border: 1px solid #ccd0d5; border-radius: 7px; padding: 9px 14px; background: #fff; cursor: pointer; font: inherit; }}
    .players-page {{ max-width: 1400px; }}
    .search {{ margin: 14px 0; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid #e1e5e9; border-radius: 10px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 0; }}
    th, td {{ border-bottom: 1px solid #e1e5e9; padding: 8px; text-align: right; vertical-align: middle; }}
    th {{ background: #f4f5f7; white-space: nowrap; font-size: 13px; }}
    tr:last-child td {{ border-bottom: 0; }}
    td.english {{ direction: ltr; text-align: left; white-space: nowrap; color: #59636e; }}
    td input {{ width: 150px; box-sizing: border-box; padding: 8px; border: 1px solid #ccd0d5; border-radius: 7px; font: inherit; }}
    a {{ color: #168acd; }}
    @media (max-width: 560px) {{
      main {{ margin-top: 14px; padding-inline: 10px; }}
      .card {{ padding: 14px; }}
      /* Mobile browsers own the copy/paste selection popover and do not let
         pages suppress it. Keep article formatting controls away from that
         popover, in a persistent bottom bar instead of above the editor. */
      #edit-form {{ padding-bottom: 72px; }}
      .toolbar {{
        position: fixed;
        z-index: 10;
        right: 10px;
        bottom: calc(10px + env(safe-area-inset-bottom));
        left: 10px;
        flex-wrap: nowrap;
        overflow-x: auto;
        margin: 0;
        padding: 6px;
        background: #fff;
        border: 1px solid #ccd0d5;
        border-radius: 10px;
        box-shadow: 0 3px 14px #0002;
        -webkit-overflow-scrolling: touch;
      }}
      .toolbar button {{ flex: 0 0 auto; }}
      .admin-row {{ align-items: flex-start; gap: 8px; }}
      .admin-actions {{ gap: 4px; }}
      .admin-edit, .admin-hide {{ min-width: 38px; min-height: 38px; box-sizing: border-box; padding: 8px; display: grid; place-items: center; }}
      .admin-action-text {{ display: none; }}
      .admin-action-icon {{ display: inline; font-size: 18px; line-height: 1; }}
    }}
  </style>
</head>
<body><main{main_class}><div class="card">{body}</div></main></body>
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
  const blockTags = new Set(['address', 'article', 'blockquote', 'div', 'figure', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'ol', 'p', 'pre', 'section', 'ul']);
  editor.querySelectorAll('h1, h2, h3, h4, h5, h6').forEach(heading => {{
    let anchor = heading;
    Array.from(heading.children).forEach(child => {{
      if (!blockTags.has(child.tagName.toLowerCase())) return;
      anchor.parentNode.insertBefore(child, anchor.nextSibling);
      anchor = child;
    }});
  }});
  document.getElementById('content').value = editor.innerHTML;
  if (!confirm('تغییرات در مقالهٔ فعلی ذخیره شود؟')) e.preventDefault();
}});
</script>"""
    return _page_shell("ویرایش مقاله", body)


def _admin_login_page(error: str = "", *, action: str = _ADMIN_LOGIN_PATH) -> str:
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    title = "ورود به ویرایش نام بازیکنان" if action == _PLAYERS_LOGIN_PATH else "مدیریت مقالات Telegraph"
    body = f"""
<h1>{title}</h1>
{error_html}
<form method="post" action="{html.escape(action, quote=True)}">
  <label for="password">رمز عبور</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button class="save" type="submit">ورود</button>
</form>
"""
    return _page_shell(title, body)


def _players_page(players: list[dict], message: str = "") -> str:
    rows = []
    for player in players:
        player_id = int(player["id"])

        def value(field: str) -> str:
            return html.escape(str(player.get(field) or ""), quote=True)

        rows.append(
            f"""
            <tr>
              <td><input name="web_name_fa" value="{value('web_name_fa')}" autocomplete="off"></td>
              <td><input name="second_name_fa" value="{value('second_name_fa')}" autocomplete="off"></td>
              <td><input name="first_name_fa" value="{value('first_name_fa')}" autocomplete="off"><input type="hidden" name="player_id" value="{player_id}"></td>
              <td class="english" dir="ltr">{value('web_name')}</td>
              <td class="english" dir="ltr">{value('second_name')}</td>
              <td class="english" dir="ltr">{value('first_name')}</td>
              <td><input name="alias" value="{value('alias')}" autocomplete="off" dir="ltr" placeholder="Alias 1, Alias 2"></td>
            </tr>
            """
        )
    message_html = f'<p class="success">{html.escape(message)}</p>' if message else ""
    body = f"""
<h1>ویرایش نام بازیکنان</h1>
<p class="note">نام‌های انگلیسی فقط برای مرجع نمایش داده می‌شوند. نام‌های مستعار را با کاما جدا کنید؛ تغییرات مستقیماً در پایگاه‌دادهٔ تولیدی ذخیره می‌شوند و پس از استقرار مجدد نیز باقی می‌مانند.</p>
{message_html}
<input id="player-search" class="search" type="search" placeholder="جست‌وجوی بازیکن" autocomplete="off">
<form method="post" action="{_PLAYERS_PATH}" id="players-form">
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>نام نمایشی فارسی</th><th>نام خانوادگی فارسی</th><th>نام فارسی</th>
        <th>نام نمایشی انگلیسی</th><th>نام خانوادگی انگلیسی</th><th>نام انگلیسی</th><th>نام‌های مستعار انگلیسی</th>
      </tr></thead>
      <tbody id="player-rows">{"".join(rows) or '<tr><td colspan="7">بازیکنی پیدا نشد.</td></tr>'}</tbody>
    </table>
  </div>
  <button class="save" type="submit">ذخیرهٔ تغییرات</button>
</form>
<form method="post" action="{_ADMIN_LOGOUT_PATH}"><button class="logout" type="submit">خروج</button></form>
<script>
const search = document.getElementById('player-search');
search.addEventListener('input', () => {{
  const term = search.value.trim().toLowerCase();
  document.querySelectorAll('#player-rows tr').forEach(row => {{
    row.hidden = term && !row.innerText.toLowerCase().includes(term);
  }});
}});
</script>
"""
    return _page_shell("ویرایش نام بازیکنان", body, page_class="players-page")


def _admin_index_page(pages: list[dict], hidden_paths: set[str] | None = None) -> str:
    hidden_paths = hidden_paths or set()
    rows = []
    for page in pages:
        title = html.escape(str(page.get("title") or "بدون عنوان"))
        page_url = str(page.get("url") or "").strip()
        safe_page_url = html.escape(page_url, quote=True)
        editor_url = html.escape(create_edit_url(page_url), quote=True)
        page_path = page_url.rstrip("/").rsplit("/", 1)[-1]
        is_hidden = page_path in hidden_paths
        hidden_class = " hidden" if is_hidden else ""
        visibility_label = "نمایش در کاتالوگ" if is_hidden else "مخفی کردن از کاتالوگ"
        visibility_value = "0" if is_hidden else "1"
        rows.append(
            f"""
            <article class="admin-row{hidden_class}">
              <div class="admin-title"><h2>{title}</h2><p class="note">{safe_page_url}</p></div>
              <div class="admin-actions">
                <a class="admin-edit" href="{editor_url}" target="_blank" rel="noopener" aria-label="ویرایش" title="ویرایش"><span class="admin-action-text">ویرایش</span><span class="admin-action-icon" aria-hidden="true">✎</span></a>
                <form class="admin-action" method="post" action="{_ADMIN_HIDE_PATH}">
                  <input type="hidden" name="url" value="{safe_page_url}">
                  <input type="hidden" name="hidden" value="{visibility_value}">
                  <button class="admin-hide" type="submit" aria-label="{visibility_label}" title="{visibility_label}"><span class="admin-action-text">{visibility_label}</span><span class="admin-action-icon" aria-hidden="true">{'◉' if is_hidden else '⊘'}</span></button>
                </form>
              </div>
            </article>
            """
        )
    body = f"""
<h1>مدیریت مقالات Telegraph</h1>
<p class="note">برای تغییر عنوان یا متن، روی «ویرایش» بزنید. لینک ویرایش موقت است و پس از ذخیره منقضی می‌شود.</p>
<p class="note">دکمهٔ «مخفی کردن از کاتالوگ» فقط مقاله را از صفحهٔ اصلی این دامنه پنهان می‌کند؛ خود مقالهٔ Telegraph و لینک مستقیم آن باقی می‌ماند. هر زمان بخواهید می‌توانید آن را دوباره نمایش دهید.</p>
<div class="admin-list">{"".join(rows) or '<p class="note">مقاله‌ای پیدا نشد.</p>'}</div>
<form method="post" action="{_ADMIN_LOGOUT_PATH}"><button class="logout" type="submit">خروج</button></form>
"""
    return _page_shell("مدیریت مقالات", body)


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
  <link rel="icon" href="/fav-icon.png" type="image/png">
  <title>مقالات فوتبال فانتزی لیگ برتر انگلیس FPL</title>
  <style>
    :root {{ color-scheme: light; font-family: Tahoma, system-ui, sans-serif; }}
    body {{ margin: 0; background: #f3f5f7; color: #18202a; }}
    header {{ background: #310b34; color: white; padding: 24px 18px 22px; }}
    .wrap {{ max-width: 1120px; margin: 0 auto; }}
    .brand {{ display: flex; align-items: center; gap: 16px; }}
    .logo {{ display: block; width: 58px; height: 72px; object-fit: contain; flex: 0 0 auto; }}
    header h1 {{ margin: 0 0 8px; font-size: 27px; }}
    header p {{ margin: 0; color: #d8e3ea; }}
    header a {{ color: white; }}
    .league-invite {{ margin-top: 10px; }}
    .league-invite a {{ font-weight: 700; text-decoration: none; }}
    form {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 22px auto; }}
    input, select, button {{ border: 1px solid #ccd4dc; border-radius: 8px; padding: 11px 12px; font: inherit; }}
    input {{ flex: 1 1 260px; min-width: 180px; }}
    select {{ min-width: 170px; background: white; }}
    button {{ background: #02eefe; border-color: #02eefe; color: #310b34; cursor: pointer; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; padding-bottom: 24px; }}
    .card {{ overflow: hidden; background: white; border: 1px solid #e2e7eb; border-radius: 14px; box-shadow: 0 2px 10px #182b3a0d; }}
    .thumb {{ display: block; width: 100%; height: auto; object-fit: contain; background: #e7edf1; }}
    .card-body {{ padding: 16px; }}
    .meta {{ display: flex; justify-content: space-between; gap: 8px; color: #687582; font-size: 12px; }}
    .tag {{ color: #126a9e; font-weight: 700; }}
    a {{ color: #310b34; }}
    h2 {{ font-size: 19px; line-height: 1.55; margin: 10px 0 8px; }}
    h2 a {{ color: #310b34; text-decoration: none; }}
    .card p {{ color: #53616e; line-height: 1.8; min-height: 52px; margin: 0 0 12px; }}
    .read {{ color: #310b34; font-weight: 700; text-decoration: none; }}
    .pager-row {{ display: flex; justify-content: space-between; min-height: 44px; }}
    .pager {{ color: #310b34; font-weight: 700; text-decoration: none; }}
    .empty {{ background: white; border-radius: 12px; padding: 32px; text-align: center; color: #687582; }}
    @media (max-width: 600px) {{ header h1 {{ font-size: 22px; }} .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header><div class="wrap brand">
    <img class="logo" src="/logo.webp" alt="EPL Fantasy">
    <div><h1>مقالات فوتبال فانتزی لیگ برتر انگلیس <a href="https://fantasy.premierleague.com/" target="_blank" rel="noopener">FPL</a></h1><p>آرشیو مقالات منتشر شده در کانال تلگرامی <a href="https://t.me/EPL_Fantasy" target="_blank" rel="noopener">@EPL_Fantasy</a></p><p class="league-invite"><a href="https://fantasy.premierleague.com/leagues/auto-join/316d22" target="_blank" rel="noopener">برای عضویت در لیگ فانتزی ما کلیک کنید</a></p></div>
  </div></header>
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


async def _send_response(
    writer,
    status: int,
    body: str | bytes,
    *,
    head: bool = False,
    content_type: str = "text/html; charset=utf-8",
    extra_headers: tuple[str, ...] = (),
) -> None:
    reasons = {
        200: "OK", 303: "See Other", 400: "Bad Request", 401: "Unauthorized",
        404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
        500: "Internal Server Error",
    }
    payload = body if isinstance(body, bytes) else body.encode("utf-8")
    headers = (
        f"HTTP/1.1 {status} {reasons.get(status, '')}\r\n"
        f"Content-Type: {content_type}\r\n"
        "Cache-Control: no-store\r\n"
        "Referrer-Policy: no-referrer\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "X-Frame-Options: DENY\r\n"
        "Content-Security-Policy: default-src 'none'; img-src 'self' https: data:; "
        "frame-src https:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'\r\n"
        + "".join(f"{header}\r\n" for header in extra_headers)
        + f"Content-Length: {len(payload)}\r\n"
        + "Connection: close\r\n\r\n"
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
        if path in _STATIC_ASSETS and method in {"GET", "HEAD"}:
            asset_path, content_type = _STATIC_ASSETS[path]
            try:
                asset = await asyncio.to_thread(asset_path.read_bytes)
            except FileNotFoundError:
                await _send_response(writer, 404, "Not found")
                return
            await _send_response(
                writer, 200, asset, head=method == "HEAD", content_type=content_type
            )
            return
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
        if path in {_PLAYERS_PATH, _PLAYERS_PATH + "/"}:
            if not _admin_password():
                await _send_response(
                    writer, 404,
                    _message_page("ویرایش بازیکنان فعال نیست؛ TELEGRAPH_EDITOR_PASSWORD را تنظیم کنید."),
                    head=method == "HEAD",
                )
                return
            if method in {"GET", "HEAD"}:
                if not _admin_authenticated(headers):
                    await _send_response(
                        writer, 200,
                        _admin_login_page(action=_PLAYERS_LOGIN_PATH),
                        head=method == "HEAD",
                    )
                    return
                try:
                    players = await asyncio.to_thread(database.list_players_for_edit)
                except Exception as exc:
                    logger.exception("Failed to load players for editor")
                    await _send_response(
                        writer, 500,
                        _message_page(f"دریافت فهرست بازیکنان ناموفق بود: {exc}"),
                        head=method == "HEAD",
                    )
                    return
                query = parse_qs(urlparse(target).query)
                saved = query.get("saved", [""])[0]
                message = f"{saved} نام بازیکن ذخیره شد." if saved.isdigit() else ""
                await _send_response(
                    writer, 200, _players_page(players, message), head=method == "HEAD"
                )
                return
            if method != "POST":
                await _send_response(writer, 405, "Method not allowed")
                return
            if not _admin_authenticated(headers):
                await _send_response(
                    writer, 401,
                    _admin_login_page("ابتدا وارد شوید.", action=_PLAYERS_LOGIN_PATH),
                )
                return
            if not headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
                await _send_response(writer, 400, _message_page("فرمت درخواست نامعتبر است."))
                return
            try:
                fields = parse_qs(
                    body.decode("utf-8"), keep_blank_values=True, max_num_fields=10000
                )
                player_ids = fields.get("player_id", [])
                first_names = fields.get("first_name_fa", [])
                second_names = fields.get("second_name_fa", [])
                web_names = fields.get("web_name_fa", [])
                aliases = fields.get("alias", [])
                if not (
                    player_ids
                    and len(player_ids) == len(first_names)
                    and len(player_ids) == len(second_names)
                    and len(player_ids) == len(web_names)
                    and len(player_ids) == len(aliases)
                ):
                    raise ValueError("اطلاعات نام بازیکنان ناقص است.")
                updates = [
                    (int(player_id), first_name, second_name, web_name, alias)
                    for player_id, first_name, second_name, web_name, alias in zip(
                        player_ids, first_names, second_names, web_names, aliases
                    )
                ]
                changed = await asyncio.to_thread(database.update_player_farsi_names, updates)
                # Translations resolve Persian names through a cached index, so
                # an edit made here has to take effect on the next post.
                await asyncio.to_thread(player_names.reload)
            except Exception as exc:
                logger.exception("Failed to save player names")
                await _send_response(writer, 400, _message_page(f"ذخیره نام‌ها ناموفق بود: {exc}"))
                return
            await _send_response(
                writer, 303, "", extra_headers=(f"Location: {_PLAYERS_PATH}?saved={changed}",)
            )
            return
        if path in {_ADMIN_PATH, _ADMIN_PATH + "/"}:
            if not _admin_password():
                await _send_response(
                    writer, 404,
                    _message_page("مدیریت وب فعال نیست؛ TELEGRAPH_EDITOR_PASSWORD را تنظیم کنید."),
                )
                return
            if method != "GET":
                await _send_response(writer, 405, "Method not allowed")
                return
            if not _admin_authenticated(headers):
                await _send_response(writer, 200, _admin_login_page())
                return
            try:
                pages = await asyncio.to_thread(articles.get_recent_telegraph_pages, 100)
                hidden_paths = await asyncio.to_thread(article_catalog.hidden_paths)
                page = _admin_index_page(pages, hidden_paths)
            except Exception as exc:
                logger.exception("Failed to load Telegraph admin pages")
                await _send_response(writer, 500, _message_page(f"دریافت فهرست مقالات ناموفق بود: {exc}"))
                return
            await _send_response(writer, 200, page)
            return
        if path == _ADMIN_HIDE_PATH:
            if not _admin_password():
                await _send_response(writer, 404, "Not found")
                return
            if method != "POST":
                await _send_response(writer, 405, "Method not allowed")
                return
            if not _admin_authenticated(headers):
                await _send_response(writer, 401, _admin_login_page("ابتدا وارد مدیریت شوید."))
                return
            if not headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
                await _send_response(writer, 400, _message_page("فرمت درخواست نامعتبر است."))
                return
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
            page_url = fields.get("url", [""])[0].strip()
            hidden = fields.get("hidden", ["1"])[0] == "1"
            parsed_page_url = urlparse(page_url)
            if (
                parsed_page_url.scheme not in {"http", "https"}
                or parsed_page_url.hostname not in {
                    "telegra.ph", "www.telegra.ph", "graph.org", "www.graph.org",
                }
                or not parsed_page_url.path.strip("/")
            ):
                await _send_response(writer, 400, _message_page("آدرس مقالهٔ Telegraph نامعتبر است."))
                return
            changed = await asyncio.to_thread(article_catalog.set_hidden, page_url, hidden)
            if not changed:
                await _send_response(writer, 404, _message_page("این مقاله در کاتالوگ پیدا نشد."))
                return
            await _send_response(
                writer, 303, "",
                extra_headers=(f"Location: {_ADMIN_PATH}",),
            )
            return
        if path in {_ADMIN_LOGIN_PATH, _PLAYERS_LOGIN_PATH}:
            if not _admin_password():
                await _send_response(writer, 404, "Not found")
                return
            if method != "POST":
                await _send_response(writer, 405, "Method not allowed")
                return
            if not headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
                await _send_response(
                    writer, 400,
                    _admin_login_page(
                        "فرمت درخواست نامعتبر است.",
                        action=path,
                    ),
                )
                return
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
            password = fields.get("password", [""])[0]
            if not secrets.compare_digest(password, _admin_password()):
                await _send_response(
                    writer, 401,
                    _admin_login_page("رمز عبور نادرست است.", action=path),
                )
                return
            now = time.time()
            for token, expires_at in list(_admin_sessions.items()):
                if expires_at <= now:
                    _admin_sessions.pop(token, None)
            token = secrets.token_urlsafe(32)
            _admin_sessions[token] = now + _ADMIN_SESSION_LIFETIME_SECONDS
            await _send_response(
                writer, 303, "",
                extra_headers=(
                    f"Location: {_PLAYERS_PATH if path == _PLAYERS_LOGIN_PATH else _ADMIN_PATH}",
                    f"Set-Cookie: {_admin_cookie(token, max_age=_ADMIN_SESSION_LIFETIME_SECONDS)}",
                ),
            )
            return
        if path == _ADMIN_LOGOUT_PATH:
            if method != "POST":
                await _send_response(writer, 405, "Method not allowed")
                return
            _admin_sessions.pop(_admin_session_from_headers(headers), None)
            await _send_response(
                writer, 303, "",
                extra_headers=(
                    f"Location: {_ADMIN_PATH}",
                    f"Set-Cookie: {_admin_cookie('', max_age=0)}",
                ),
            )
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
            await asyncio.to_thread(article_catalog.update_title, grant.page_url, title)
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
