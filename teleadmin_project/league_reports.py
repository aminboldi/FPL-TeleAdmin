"""Official FPL classic-league reports, with a short in-process cache."""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


_CACHE: dict[str, tuple[float, dict]] = {}
_ACTIVITY_CACHE: dict[str, tuple[float, str]] = {}
_TTL_SECONDS = 15 * 60


class LeagueError(Exception):
    pass


def _fetch_all(league_id: str) -> dict:
    cached = _CACHE.get(league_id)
    if cached and time.monotonic() - cached[0] < _TTL_SECONDS:
        return cached[1]

    rows = []
    new_entries = []
    league = None
    page = 1
    while True:
        response = requests.get(
            f"https://fantasy.premierleague.com/api/leagues-classic/{league_id}/standings/",
            params={"page_standings": page, "page_new_entries": 1}, timeout=25,
        )
        if response.status_code == 404:
            raise LeagueError("League not found or its ID is not accessible.")
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LeagueError(f"Could not fetch league standings: {exc}") from exc
        payload = response.json()
        league = league or payload.get("league", {})
        standings = payload.get("standings", {})
        rows.extend(standings.get("results", []))
        if not standings.get("has_next"):
            break
        page += 1
        if page > 200:  # defensive bound against an unexpected API response
            raise LeagueError("League is too large to summarise safely in one request.")

    # Before GW1, FPL puts every joined manager in ``new_entries`` while the
    # standings list is empty. It is independently paginated from standings.
    page = 1
    while True:
        response = requests.get(
            f"https://fantasy.premierleague.com/api/leagues-classic/{league_id}/standings/",
            params={"page_standings": 1, "page_new_entries": page}, timeout=25,
        )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LeagueError(f"Could not fetch league members: {exc}") from exc
        payload = response.json()
        league = league or payload.get("league", {})
        entries = payload.get("new_entries", {})
        new_entries.extend(entries.get("results", []))
        if not entries.get("has_next"):
            break
        page += 1
        if page > 200:
            raise LeagueError("League is too large to count safely in one request.")

    member_entries = {
        row["entry"] for row in rows + new_entries if row.get("entry")
    }
    result = {
        "league": league or {},
        "rows": rows,
        "member_entries": member_entries,
        "preseason_entries": new_entries,
    }
    _CACHE[league_id] = (time.monotonic(), result)
    return result


def build_summary(league_id: str, top_n: int = 10) -> str:
    data = _fetch_all(league_id)
    rows = data["rows"]
    league_name = data["league"].get("name", "لیگ")
    top = sorted(rows, key=lambda row: row.get("rank", 10**9))[:top_n]
    weekly = sorted(rows, key=lambda row: row.get("event_total", 0), reverse=True)[:3]
    lines = [f"<b>📊 {league_name}</b>", f"تعداد اعضا: <b>{len(data['member_entries'])}</b>", ""]
    if not rows and data["preseason_entries"]:
        lines.append("<i>جدول پس از شروع GW1 نمایش داده می‌شود.</i>")
        return "\n".join(lines)
    lines.append("<b>🏆 جدول</b>")
    for row in top:
        movement = row.get("last_rank", row.get("rank", 0)) - row.get("rank", 0)
        arrow = "🟢" if movement > 0 else "🔴" if movement < 0 else "⚪"
        lines.append(
            f"<blockquote><b>{row.get('rank')}</b>. {row.get('entry_name', '—')} "
            f"— <b>{row.get('total', 0)}</b> {arrow}</blockquote>"
        )
    if weekly:
        lines.extend(["", "<b>⭐ بالاترین امتیاز هفته</b>"])
        for row in weekly:
            lines.append(
                f"<blockquote>{row.get('entry_name', '—')} — <b>{row.get('event_total', 0)}</b></blockquote>"
            )
    return "\n".join(lines)


def _current_event() -> int | None:
    response = requests.get("https://fantasy.premierleague.com/api/bootstrap-static/", timeout=25)
    response.raise_for_status()
    for event in response.json().get("events", []):
        if event.get("is_current"):
            return event["id"]
    return None


def _entry_engaged(entry_id: int, event_id: int) -> bool:
    response = requests.get(
        f"https://fantasy.premierleague.com/api/entry/{entry_id}/history/", timeout=20
    )
    response.raise_for_status()
    payload = response.json()
    current = next((row for row in payload.get("current", []) if row.get("event") == event_id), {})
    used_chip = any(row.get("event") == event_id for row in payload.get("chips", []))
    return bool(current.get("event_transfers", 0) or used_chip)


def build_activity(league_id: str) -> str:
    """Count managers who made a transfer or used a chip in the current GW.

    This is intentionally called "engaged" rather than "active": a manager can
    make a deliberate no-transfer decision, which public FPL data cannot detect.
    """
    cached = _ACTIVITY_CACHE.get(league_id)
    if cached and time.monotonic() - cached[0] < _TTL_SECONDS:
        return cached[1]
    event_id = _current_event()
    if not event_id:
        raise LeagueError("هنوز گیم‌ویک فعالی در FPL وجود ندارد.")
    data = _fetch_all(league_id)
    entries = list(data["member_entries"])
    if not entries:
        raise LeagueError("No league members were returned.")
    engaged = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_entry_engaged, entry_id, event_id) for entry_id in entries]
        for future in as_completed(futures):
            try:
                engaged += int(future.result())
            except requests.RequestException:
                failed += 1
    checked = len(entries) - failed
    if not checked:
        raise LeagueError("Could not retrieve member history data.")
    percent = engaged / checked * 100
    suffix = f"\nدادهٔ {failed} عضو در دسترس نبود." if failed else ""
    text = (
        f"<b>📈 مشارکت GW{event_id}</b>\n\n"
        f"عضوِ دارای انتقال یا چیپ: <b>{engaged}</b> از <b>{checked}</b> "
        f"(<b>{percent:.1f}%</b>)\n\n"
        "<i>این معیار «درگیر بودن» است؛ تصمیم آگاهانه برای انتقال ندادن قابل تشخیص نیست.</i>"
        f"{suffix}"
    )
    _ACTIVITY_CACHE[league_id] = (time.monotonic(), text)
    return text
