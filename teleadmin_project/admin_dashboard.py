"""Private, admin-only BotFather control surface for TeleAdmin."""
import asyncio
import secrets
from collections.abc import Awaitable, Callable

from telethon import Button, events

import league_reports
import runtime_config


class AdminDashboard:
    def __init__(
        self,
        client,
        admin_ids: set[int],
        x_preview: Callable[[str], Awaitable[str]],
        youtube_import: Callable[[str], Awaitable[str]],
        openrouter_status: Callable[[], Awaitable[str]],
        content_preview: Callable[[str], Awaitable[str]],
        content_publish: Callable[[], Awaitable[str]],
        youtube_add: Callable[[str, int], Awaitable[str]],
        youtube_remove: Callable[[str, int], Awaitable[str]],
        youtube_list: Callable[[], str],
        source_add: Callable[[str, int], Awaitable[str]],
        source_remove: Callable[[str, int], Awaitable[str]],
        source_list: Callable[[], str],
        translate_submission: Callable[[object], Awaitable[str]],
        telegraph_authorize: Callable[[], Awaitable[str]],
        article_import: Callable[[str], Awaitable[str]],
    ):
        self.client = client
        self.admin_ids = admin_ids
        self.x_preview = x_preview
        self.youtube_import = youtube_import
        self.openrouter_status = openrouter_status
        self.content_preview = content_preview
        self.content_publish = content_publish
        self.youtube_add = youtube_add
        self.youtube_remove = youtube_remove
        self.youtube_list = youtube_list
        self.source_add = source_add
        self.source_remove = source_remove
        self.source_list = source_list
        self.translate_submission = translate_submission
        self.telegraph_authorize = telegraph_authorize
        self.article_import = article_import
        self.pending: dict[str, tuple[str, str, int]] = {}
        self.pending_youtube: dict[str, tuple[str, str, int]] = {}
        self.pending_source: dict[str, tuple[str, str, int]] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.client.on(events.NewMessage(incoming=True))
        async def on_message(event):
            if not self._allowed(event):
                return
            await self._handle_command(event)

        @self.client.on(events.CallbackQuery)
        async def on_callback(event):
            if event.sender_id not in self.admin_ids:
                await event.answer("دسترسی ندارید.", alert=True)
                return
            await self._handle_callback(event)

    def _allowed(self, event) -> bool:
        return bool(event.is_private and event.sender_id in self.admin_ids)

    @staticmethod
    def _dashboard_buttons():
        return [
            [Button.inline("📊 لیگ‌ها", b"menu:leagues"), Button.inline("⚽ محتوا", b"menu:content")],
            [Button.inline("📡 منابع تلگرام", b"source:list"), Button.inline("📺 پایش YouTube", b"youtube:list")],
            [Button.inline("📤 X post", b"xhelp")],
            [Button.inline("⚙️ مدیریت", b"menu:settings")],
            [Button.inline("💳 اعتبار APIها", b"openrouter"), Button.inline("❔ راهنما", b"guide")],
        ]

    @staticmethod
    def _back_button():
        return [[Button.inline("‹ بازگشت", b"back")]]

    @staticmethod
    def _main_text() -> str:
        return "<b>پنل مدیریت TeleAdmin</b>\nاز گزینه‌های زیر استفاده کنید."

    async def _handle_command(self, event) -> None:
        text = (event.raw_text or "").strip()
        # Accept the compact dashboard form as well as the standard Telegram
        # Compact forms: x/https://x.com/... and y/https://youtube.com/...
        if text.lower().startswith("x/http://") or text.lower().startswith("x/https://"):
            text = "/x " + text[2:]
        elif text.lower().startswith("y/http://") or text.lower().startswith("y/https://"):
            text = "/y " + text[2:]
        elif text.lower().startswith("a/http://") or text.lower().startswith("a/https://"):
            text = "/a " + text[2:]
        if not text.startswith("/"):
            await event.reply("در حال ترجمه و آماده‌سازی پست…")
            try:
                result = await self.translate_submission(event)
            except Exception as exc:
                await event.reply(f"❌ {exc}")
            else:
                await event.reply(result, parse_mode="html")
            return
        command, _, arg = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        arg = arg.strip()

        if command in {"/start", "/dashboard", "/help"}:
            await event.reply(
                self._main_text(),
                buttons=self._dashboard_buttons(), parse_mode="html",
            )
        elif command == "/guide":
            await event.reply(self._guide_text(), buttons=self._back_button(), parse_mode="html")
        elif command == "/edit":
            await self._telegraph_edit(event)
        elif command == "/a":
            if not arg.startswith(("http://", "https://")):
                await event.reply("نمونه: <code>/a https://example.com/article</code>", parse_mode="html")
            else:
                await event.reply("در حال استخراج نسخهٔ خواندنی و ترجمهٔ مقاله…")
                try:
                    result = await self.article_import(arg)
                except Exception as exc:
                    await event.reply(f"❌ {exc}")
                else:
                    await event.reply(result, parse_mode="html")
        elif command == "/channels":
            await event.reply(self._channels_text(), buttons=[[Button.inline("تغییر مقصد", b"targethelp")]], parse_mode="html")
        elif command == "/target":
            if not arg.startswith("@") and not arg.lstrip("-").isdigit():
                await event.reply("شناسه کانال باید با @ شروع شود یا یک شناسه عددی باشد.")
            else:
                await self._propose(event, "TARGET_CHANNEL_ID", arg)
        elif command == "/league":
            await self._league(event, arg or "epl")
        elif command == "/activity":
            await self._activity(event, arg or "epl")
        elif command in {"/balance", "/openrouter"}:
            await self._openrouter(event)
        elif command in {"/fixtures", "/points", "/eo", "/prices", "/lineups"}:
            await self._content(event, command[1:])
        elif command == "/set":
            key, _, value = arg.partition(" ")
            await self._set_command(event, key.upper(), value)
        elif command == "/x":
            if not arg:
                await event.reply("نمونه: <code>/x https://x.com/account/status/123</code>", parse_mode="html")
            else:
                await event.reply("در حال دریافت پست X و ساخت پیش‌نمایش…")
                try:
                    preview = await self.x_preview(arg)
                except Exception as exc:
                    await event.reply(f"❌ {exc}")
                else:
                    await event.reply(preview, parse_mode="html")
        elif command == "/y":
            if not arg:
                await event.reply("نمونه: <code>y/https://youtube.com/watch?v=...</code>", parse_mode="html")
            else:
                await event.reply("در حال دریافت زیرنویس و آماده‌سازی پست فارسی از YouTube…")
                try:
                    result = await self.youtube_import(arg)
                except Exception as exc:
                    await event.reply(f"❌ {exc}")
                else:
                    await event.reply(result, parse_mode="html")
        elif command == "/youtube":
            await self._youtube_command(event, arg)
        elif command == "/source":
            await self._source_command(event, arg)

    async def _handle_callback(self, event) -> None:
        data = event.data.decode("utf-8")
        if data == "channels":
            await event.edit(self._channels_text(), buttons=[[Button.inline("تغییر مقصد", b"targethelp")]], parse_mode="html")
        elif data == "targethelp":
            await event.answer("از /target @channel استفاده کنید.", alert=True)
        elif data == "xhelp":
            await event.answer("لینک را با /x ارسال کنید.", alert=True)
        elif data == "youtube:list":
            await event.edit(self.youtube_list(), buttons=self._back_button(), parse_mode="html")
        elif data == "source:list":
            await event.edit(self.source_list(), buttons=self._back_button(), parse_mode="html")
        elif data == "back":
            await event.edit(self._main_text(), buttons=self._dashboard_buttons(), parse_mode="html")
        elif data == "guide":
            await event.edit(self._guide_text(), buttons=self._back_button(), parse_mode="html")
        elif data == "menu:leagues":
            await event.edit(
                "<b>📊 گزارش لیگ‌ها</b>",
                buttons=[
                    [Button.inline("🏆 جدول لیگ کانال", b"league:epl"), Button.inline("🇮🇷 جدول لیگ ایران", b"league:iran")],
                    [Button.inline("📈 فعالیت لیگ کانال", b"activity:epl"), Button.inline("📈 فعالیت لیگ ایران", b"activity:iran")],
                    *self._back_button(),
                ], parse_mode="html",
            )
        elif data == "menu:content":
            await event.edit(
                "<b>⚽ تولید محتوا</b>\nهمهٔ گزینه‌ها بدون OpenRouter ساخته می‌شوند.",
                buttons=[
                    [Button.inline("📅 بازی‌های هفته", b"content:fixtures"), Button.inline("📊 امتیازات آخرین بازی", b"content:points")],
                    [Button.inline("👥 EO", b"content:eo"), Button.inline("💷 پیش‌بینی قیمت", b"content:prices")],
                    [Button.inline("📋 وضعیت ترکیب‌ها", b"content:lineups")],
                    *self._back_button(),
                ], parse_mode="html",
            )
        elif data == "menu:settings":
            await event.edit(
                self._channels_text() + "\n\nبرای سایر تنظیمات: <code>/set KEY VALUE</code>",
                buttons=[[Button.inline("تغییر مقصد", b"targethelp")], *self._back_button()], parse_mode="html",
            )
        elif data == "openrouter":
            await event.edit("در حال بررسی اعتبار OpenRouter…")
            try:
                text = await self.openrouter_status()
            except Exception as exc:
                text = f"❌ {exc}"
            await event.edit(text, buttons=self._dashboard_buttons(), parse_mode="html")
        elif data.startswith("league:"):
            await event.edit("در حال دریافت جدول لیگ…")
            try:
                text = await self._league_text(data.split(":", 1)[1])
            except Exception as exc:
                text = f"❌ {exc}"
            await event.edit(text, buttons=self._dashboard_buttons(), parse_mode="html")
        elif data.startswith("activity:"):
            await event.edit("در حال بررسی فعالیت اعضا…")
            try:
                text = await self._activity_text(data.split(":", 1)[1])
            except Exception as exc:
                text = f"❌ {exc}"
            await event.edit(text, buttons=self._dashboard_buttons(), parse_mode="html")
        elif data.startswith("content:"):
            await event.edit("در حال ساخت پیش‌نمایش…")
            kind = data.split(":", 1)[1]
            try:
                text = await self.content_preview(kind)
            except Exception as exc:
                text = f"❌ {exc}"
                buttons = self._back_button()
            else:
                buttons = self._back_button() if kind == "lineups" else [
                    [Button.inline("انتشار در کانال", b"contentpublish")], *self._back_button()
                ]
            await event.edit(text, buttons=buttons, parse_mode="html")
        elif data == "audit":
            rows = runtime_config.recent_audit()
            text = "<b>🧾 تغییرات اخیر</b>\n\n" + ("\n".join(
                f"<blockquote>{r['key']}: {r['old_value'] or '—'} → {r['new_value'] or '—'}</blockquote>" for r in rows
            ) if rows else "تغییری ثبت نشده است.")
            await event.edit(text, buttons=self._dashboard_buttons(), parse_mode="html")
        elif data.startswith("confirm:"):
            token = data.split(":", 1)[1]
            proposed = self.pending.pop(token, None)
            if not proposed or proposed[2] != event.sender_id:
                await event.answer("این درخواست منقضی شده است.", alert=True)
                return
            key, value, actor_id = proposed
            runtime_config.set_value(key, value, actor_id)
            await event.edit(f"✅ <b>{key}</b> به <code>{value}</code> تغییر کرد.", parse_mode="html")
        elif data.startswith("cancel:"):
            self.pending.pop(data.split(":", 1)[1], None)
            await event.edit("لغو شد.")
        elif data == "contentpublish":
            try:
                result = await self.content_publish()
            except Exception as exc:
                await event.edit(f"❌ {exc}", buttons=self._back_button())
            else:
                await event.edit(result, buttons=self._back_button())
        elif data.startswith("youtubeconfirm:"):
            token = data.split(":", 1)[1]
            proposed = self.pending_youtube.pop(token, None)
            if not proposed or proposed[2] != event.sender_id:
                await event.answer("این درخواست منقضی شده است.", alert=True)
                return
            action, value, actor_id = proposed
            try:
                result = await (self.youtube_add(value, actor_id) if action == "add" else self.youtube_remove(value, actor_id))
            except Exception as exc:
                result = f"❌ {exc}"
            await event.edit(result, parse_mode="html")
        elif data.startswith("youtubecancel:"):
            self.pending_youtube.pop(data.split(":", 1)[1], None)
            await event.edit("لغو شد.")
        elif data.startswith("sourceconfirm:"):
            token = data.split(":", 1)[1]
            proposed = self.pending_source.pop(token, None)
            if not proposed or proposed[2] != event.sender_id:
                await event.answer("این درخواست منقضی شده است.", alert=True)
                return
            action, value, actor_id = proposed
            try:
                result = await (self.source_add(value, actor_id) if action == "add" else self.source_remove(value, actor_id))
            except Exception as exc:
                result = f"❌ {exc}"
            await event.edit(result, parse_mode="html")
        elif data.startswith("sourcecancel:"):
            self.pending_source.pop(data.split(":", 1)[1], None)
            await event.edit("لغو شد.")

    def _channels_text(self) -> str:
        values = runtime_config.values()
        return (
            "<b>📡 کانال مقصد</b>\n\n"
            f"مقصد: <code>{values['TARGET_CHANNEL_ID'] or '—'}</code>"
        )

    async def _propose(self, event, key: str, value: str) -> None:
        token = secrets.token_urlsafe(8)
        self.pending[token] = (key, value, event.sender_id)
        old = runtime_config.get(key)
        await event.reply(
            f"<b>تأیید تغییر</b>\n\n<code>{old or '—'}</code> → <code>{value}</code>",
            buttons=[[Button.inline("تأیید", f"confirm:{token}".encode()), Button.inline("لغو", f"cancel:{token}".encode())]],
            parse_mode="html",
        )

    async def _set_command(self, event, key: str, value: str) -> None:
        if key not in runtime_config.DEFAULTS:
            await event.reply("این تنظیم قابل ویرایش نیست.")
            return
        if not value:
            await event.reply("نمونه: <code>/set PRICE_PREDICTIONS_ENABLED false</code>", parse_mode="html")
            return
        if key == "TARGET_CHANNEL_ID":
            if not value.startswith("@") and not value.lstrip("-").isdigit():
                await event.reply("شناسه کانال باید با @ شروع شود یا یک شناسه عددی باشد.")
                return
        if key.endswith("_ID") and key != "TARGET_CHANNEL_ID" and not value.isdigit():
            await event.reply("شناسه لیگ باید عددی باشد.")
            return
        if key == "PRICE_PREDICTIONS_ENABLED" and value.lower() not in {"true", "false"}:
            await event.reply("مقدار باید true یا false باشد.")
            return
        await self._propose(event, key, value)

    async def _youtube_command(self, event, arg: str) -> None:
        action, _, value = arg.partition(" ")
        value = value.strip()
        if action in {"list", "ls"} or not action:
            await event.reply(self.youtube_list(), parse_mode="html")
            return
        if action not in {"add", "remove"} or not value:
            await event.reply(
                "نمونه: <code>/youtube add https://youtube.com/@channel</code>\n"
                "یا: <code>/youtube remove UC...</code>",
                parse_mode="html",
            )
            return
        token = secrets.token_urlsafe(8)
        self.pending_youtube[token] = (action, value, event.sender_id)
        verb = "افزودن" if action == "add" else "حذف"
        await event.reply(
            f"<b>تأیید {verb} کانال YouTube</b>\n\n<code>{value}</code>",
            buttons=[[
                Button.inline("تأیید", f"youtubeconfirm:{token}".encode()),
                Button.inline("لغو", f"youtubecancel:{token}".encode()),
            ]],
            parse_mode="html",
        )

    async def _source_command(self, event, arg: str) -> None:
        action, _, value = arg.partition(" ")
        value = value.strip()
        if action in {"list", "ls"} or not action:
            await event.reply(self.source_list(), parse_mode="html")
            return
        if action not in {"add", "remove"} or not value:
            await event.reply(
                "نمونه: <code>/source add @sourcechannel</code>\n"
                "یا: <code>/source remove @sourcechannel</code>",
                parse_mode="html",
            )
            return
        token = secrets.token_urlsafe(8)
        self.pending_source[token] = (action, value, event.sender_id)
        verb = "افزودن" if action == "add" else "حذف"
        await event.reply(
            f"<b>تأیید {verb} کانال منبع</b>\n\n<code>{value}</code>",
            buttons=[[
                Button.inline("تأیید", f"sourceconfirm:{token}".encode()),
                Button.inline("لغو", f"sourcecancel:{token}".encode()),
            ]],
            parse_mode="html",
        )

    async def _league_text(self, which: str) -> str:
        key = "EPL_LEAGUE_ID" if which == "epl" else "IRAN_LEAGUE_ID"
        league_id = runtime_config.get(key)
        if not league_id:
            raise ValueError(f"{key} تنظیم نشده است.")
        return await asyncio.to_thread(league_reports.build_summary, league_id)

    async def _league(self, event, which: str) -> None:
        try:
            await event.reply(await self._league_text(which.lower()), parse_mode="html")
        except Exception as exc:
            await event.reply(f"❌ {exc}")

    async def _activity_text(self, which: str) -> str:
        key = "EPL_LEAGUE_ID" if which == "epl" else "IRAN_LEAGUE_ID"
        league_id = runtime_config.get(key)
        if not league_id:
            raise ValueError(f"{key} تنظیم نشده است.")
        return await asyncio.to_thread(league_reports.build_activity, league_id)

    async def _activity(self, event, which: str) -> None:
        await event.reply("در حال بررسی فعالیت اعضا…")
        try:
            await event.reply(await self._activity_text(which.lower()), parse_mode="html")
        except Exception as exc:
            await event.reply(f"❌ {exc}")

    async def _openrouter(self, event) -> None:
        await event.reply("در حال بررسی اعتبار OpenRouter…")
        try:
            await event.reply(await self.openrouter_status(), parse_mode="html")
        except Exception as exc:
            await event.reply(f"❌ {exc}")

    async def _content(self, event, kind: str) -> None:
        await event.reply("در حال ساخت پیش‌نمایش…")
        try:
            text = await self.content_preview(kind)
        except Exception as exc:
            await event.reply(f"❌ {exc}")
            return
        buttons = self._back_button() if kind == "lineups" else [[Button.inline("انتشار در کانال", b"contentpublish")]]
        await event.reply(text, buttons=buttons, parse_mode="html")

    async def _telegraph_edit(self, event) -> None:
        try:
            auth_url = await self.telegraph_authorize()
        except Exception as exc:
            await event.reply(f"❌ {exc}")
            return
        await event.reply(
            "این لینک فقط برای شماست و تا ۵ دقیقه اعتبار دارد. آن را باز کنید، سپس "
            "مقالهٔ Telegraph موردنظر را باز کنید و ویرایش کنید.",
            buttons=[[Button.url("✏️ فعال‌سازی ویرایش Telegraph", auth_url)]],
        )

    @staticmethod
    def _guide_text() -> str:
        return (
            "<b>❔ راهنمای دستورات</b>\n\n"
            "<b>منو</b>\n/dashboard — پنل اصلی\n/guide — همین راهنما\n/balance — اعتبار OpenRouter\n\n"
            "<b>مقاله‌های Telegraph</b>\n/edit — فعال‌سازی ویرایش در مرورگر (۵ دقیقه)\n\n"
            "<b>مقاله از لینک</b>\n/a https://example.com/article\nیا a/https://example.com/article — استخراج نسخهٔ خواندنی، ترجمه و انتشار در Telegraph\n\n"
            "<b>لیگ‌ها</b>\n/league epl یا /league iran\n/activity epl یا /activity iran\n\n"
            "<b>محتوا (بدون AI)</b>\n/fixtures — برنامهٔ بازی‌های هفته\n/points — امتیازات آخرین بازی تمام‌شده\n/eo — مالکیت مؤثر\n/prices — پیش‌بینی قیمت LiveFPL\n/lineups — وضعیت دریافت خودکار ترکیب‌ها\n\n"
            "<b>انتشار از X</b>\n/x https://x.com/account/status/123\nیا x/https://x.com/account/status/123\n\n"
            "<b>انتشار از YouTube</b>\n/y https://youtube.com/watch?v=...\nیا y/https://youtu.be/...\n\n"
            "<b>ترجمهٔ مستقیم</b>\nهر متن، پست فورواردشده یا رسانه را بدون دستور ارسال کنید؛ خودکار ترجمه و در صف کانال قرار می‌گیرد.\n\n"
            "<b>پایش YouTube</b>\n/youtube — فهرست کانال‌ها\n/youtube add https://youtube.com/@channel\n/youtube remove UC...\n\n"
            "<b>منابع تلگرام</b>\n/source — فهرست کانال‌ها\n/source add @sourcechannel\n/source remove @sourcechannel\n\n"
            "<b>تنظیمات</b>\n/channels\n/target @channel\n/set PRICE_PREDICTIONS_ENABLED false\n/set EPL_LEAGUE_ID 12345\n\n"
            "تغییرات تنظیمات و انتشار محتوای داشبورد نیاز به تأیید دارند؛ پست‌های X مستقیماً برای بررسی در صف زمان‌بندی کانال قرار می‌گیرند."
        )
