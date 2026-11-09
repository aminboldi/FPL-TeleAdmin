"""Content the admin dashboard prepares on request: provider balances,
fixtures, and the pending publish-confirmation post."""
import asyncio
from datetime import datetime, timedelta, timezone

import requests


import price_changes
import database as db
import livefpl
from app import (
    settings,
)
from post_format import (
    _escape_html,
)
from delivery import (
    _send_to_target,
)


# A match plus stoppage, a delayed start, and the wait before the API marks it
# finished. A fixture unfinished for longer than this was postponed.
_FIXTURE_IN_PROGRESS_HOURS = 4


_pending_dashboard_content: str | None = None


def _fetch_openrouter_balance() -> dict:
    response = requests.get(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        timeout=25,
    )
    response.raise_for_status()
    return response.json().get("data", response.json())


def _fetch_google_aistudio_status() -> dict:
    if not settings.google_aistudio_key:
        return {"configured": False}
    try:
        response = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": settings.google_aistudio_key},
            params={"pageSize": 100},
            timeout=25,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {"configured": True, "available": False, "error": str(exc)}
    if not response.ok:
        return {
            "configured": True,
            "available": False,
            "error": data.get("error", {}).get("message", f"HTTP {response.status_code}"),
        }
    models = {
        model.get("name", "").removeprefix("models/")
        for model in data.get("models", [])
        if "generateContent" in model.get("supportedGenerationMethods", [])
    }
    return {
        "configured": True,
        "available": True,
        "model_available": settings.google_aistudio_model in models,
    }


async def _openrouter_balance() -> str:
    data, google = await asyncio.gather(
        asyncio.to_thread(_fetch_openrouter_balance),
        asyncio.to_thread(_fetch_google_aistudio_status),
    )
    limit = data.get("limit")
    usage = data.get("usage")
    remaining = data.get("limit_remaining")
    def display(value):
        return "نامحدود" if value is None else str(value)
    tier = "رایگان" if data.get("is_free_tier") else "اعتباری"
    google_status = "تنظیم نشده"
    if google.get("configured"):
        if google.get("available"):
            connection = "متصل"
            model_status = "فعال" if google.get("model_available") else "مدل پیدا نشد"
            google_status = (
                f"اتصال: <b>{connection}</b>\n"
                f"مدل: <code>{settings.google_aistudio_model}</code> ({model_status})\n"
                "مصرف/سهمیه: در API عمومی قابل دریافت نیست؛ در AI Studio قابل مشاهده است"
            )
        else:
            google_status = f"خطا: <b>{_escape_html(google.get('error', 'نامشخص'))}</b>"
    return (
        "<b>💳 اعتبار OpenRouter</b>\n\n"
        f"نوع حساب: <b>{tier}</b>\n"
        f"سقف: <b>{display(limit)}</b>\n"
        f"مصرف: <b>{display(usage)}</b>\n"
        f"مانده: <b>{display(remaining)}</b>\n\n"
        "<b>🔹 Google AI Studio</b>\n\n"
        f"{google_status}\n\n"
        '<a href="https://aistudio.google.com/">مشاهدهٔ سهمیه در Google AI Studio</a>'
    )


def _gameweek_has_football_left(gameweek_id: int) -> bool:
    """Whether this gameweek still has a match to come or in progress.

    A fixture that has been unfinished for longer than a match can last was
    postponed rather than played, and must not hold a gameweek open until the
    rescheduled date.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=_FIXTURE_IN_PROGRESS_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bool(
        db.query_scalar(
            "SELECT count(*) FROM fixtures "
            "WHERE gameweek_id=? AND finished=0 AND kickoff_time > ?",
            (gameweek_id, cutoff),
        )
    )


def _fixtures_gameweek() -> dict:
    """Return the gameweek whose fixtures are worth posting.

    The FPL API keeps a gameweek ``is_current`` until the *next* one's
    deadline, so between the final whistle and that deadline this command was
    still advertising matches that had already been played. Once the current
    gameweek has no football left in it, the next one is what people want.
    """
    current = db.query_one(
        "SELECT id, name FROM gameweeks WHERE is_current=1 ORDER BY id LIMIT 1"
    )
    if current:
        if _gameweek_has_football_left(current["id"]):
            return current
        upcoming = db.query_one(
            "SELECT g.id, g.name FROM gameweeks g "
            "JOIN fixtures f ON f.gameweek_id = g.id "
            "WHERE g.id > ? GROUP BY g.id ORDER BY g.id LIMIT 1",
            (current["id"],),
        )
        # Nothing follows the final gameweek of a season.
        return upcoming or current

    upcoming = db.query_one(
        "SELECT id, name FROM gameweeks WHERE is_next=1 ORDER BY id LIMIT 1"
    )
    if not upcoming:
        raise RuntimeError("گیم‌ویک فعلی یا بعدی پیدا نشد.")
    return upcoming


def _fixtures_text() -> str:
    gameweek = _fixtures_gameweek()
    fixtures = db.query(
        """SELECT f.kickoff_time, ht.short_name_fa AS home_fa, ht.short_name AS home_en,
                  at.short_name_fa AS away_fa, at.short_name AS away_en
           FROM fixtures f
           JOIN teams ht ON ht.id=f.team_h JOIN teams at ON at.id=f.team_a
           WHERE f.gameweek_id=? ORDER BY f.kickoff_time""",
        (gameweek["id"],),
    )
    if not fixtures:
        raise RuntimeError("بازی‌ای برای این گیم‌ویک پیدا نشد.")
    iran_offset = timedelta(hours=3, minutes=30)
    lines = [f"<b>📅 برنامهٔ {gameweek['name']}</b>", ""]
    current_day = None
    for fixture in fixtures:
        kickoff = datetime.strptime(fixture["kickoff_time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) + iran_offset
        # The post goes out weekly, so the weekday tells a reader which match
        # is which far quicker than a calendar date does.
        day = price_changes.persian_weekday(kickoff)
        if day != current_day:
            if current_day is not None:
                lines.append("")
            current_day = day
            lines.extend([f"<b>{day}</b>", ""])
        home = fixture["home_fa"] or fixture["home_en"]
        away = fixture["away_fa"] or fixture["away_en"]
        lines.append(f"<blockquote>{home} - {away} | <b>{kickoff.strftime('%H:%M')}</b></blockquote>")
    lines.extend(["", "@EPL_Fantasy"])
    return "\n".join(lines)


async def _prepare_dashboard_content(kind: str) -> str:
    global _pending_dashboard_content
    _pending_dashboard_content = None
    if kind == "fixtures":
        text = _fixtures_text()
    elif kind == "points":
        livefpl._games_cache = None
        fixtures = livefpl.get_finished_fixtures()
        if not fixtures:
            raise RuntimeError("هنوز بازی تمام‌شده‌ای برای امتیازات وجود ندارد.")
        fixture = max(fixtures, key=lambda item: item.get("kickoff_time", ""))
        text = await asyncio.to_thread(livefpl.build_game_text, fixture)
        if not text:
            raise RuntimeError("دادهٔ امتیازات این بازی از LiveFPL در دسترس نیست.")
    elif kind == "eo":
        livefpl._games_cache = None
        text = await asyncio.to_thread(livefpl.build_eo_text)
        if not text:
            raise RuntimeError("دادهٔ EO از LiveFPL در دسترس نیست.")
    elif kind == "prices":
        text = await asyncio.to_thread(livefpl.build_price_changes_text)
        if not text:
            raise RuntimeError("پیش‌بینی قیمت LiveFPL در دسترس نیست.")
    elif kind == "lineups":
        return (
            "<b>📋 ترکیب‌ها</b>\n\n"
            "ترکیب‌ها ۷۵ دقیقه پیش از شروع هر بازی از منبع رسمی لیگ برتر بررسی، "
            "با نام و قیمت فارسی فرمت و به‌صورت خودکار منتشر می‌شوند. "
            "برای این بخش انتشار دستی لازم نیست."
        )
    else:
        raise RuntimeError("نوع محتوا پشتیبانی نمی‌شود.")
    _pending_dashboard_content = text
    return f"<b>پیش‌نمایش</b>\n\n{text}"


async def _publish_dashboard_content() -> str:
    global _pending_dashboard_content
    if not _pending_dashboard_content:
        raise RuntimeError("پیش‌نمایش قابل انتشار وجود ندارد.")
    text = _pending_dashboard_content
    _pending_dashboard_content = None
    await _send_to_target(text)
    return "✅ محتوا در کانال منتشر شد."
