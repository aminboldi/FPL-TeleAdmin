"""Content the admin dashboard prepares on request: provider balances,
fixtures, and the pending publish-confirmation post."""
import asyncio

import requests

import fixtures_post
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


_pending_dashboard_content: str | None = None
# An image to send with the pending post; only the fixtures post has one.
_pending_dashboard_file: str | None = None


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


async def _prepare_dashboard_content(kind: str) -> str:
    global _pending_dashboard_content, _pending_dashboard_file
    _pending_dashboard_content = None
    _pending_dashboard_file = None
    if kind == "fixtures":
        text = fixtures_post.text(fixtures_post.current_gameweek())
        _pending_dashboard_file = str(fixtures_post.IMAGE)
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
    global _pending_dashboard_content, _pending_dashboard_file
    if not _pending_dashboard_content:
        raise RuntimeError("پیش‌نمایش قابل انتشار وجود ندارد.")
    text, file_path = _pending_dashboard_content, _pending_dashboard_file
    _pending_dashboard_content = _pending_dashboard_file = None
    await _send_to_target(text, file_path=file_path)
    return "✅ محتوا در کانال منتشر شد."
