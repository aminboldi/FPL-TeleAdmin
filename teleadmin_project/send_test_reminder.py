"""Send a test deadline reminder to the target channel."""
import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from config import load_config
from deadlines import deadline_scheduled_text

async def main():
    settings = load_config()
    session_str = os.getenv("TELETHON_SESSION_STRING")
    client = TelegramClient(
        StringSession(session_str) if session_str else "translation_session",
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start()

    # GW39 deadline: 2026-07-11T09:00:00Z → Iran: Saturday 12:30
    text = deadline_scheduled_text("2026-07-11T09:00:00Z", 39)
    print(f"Text: {text}")

    await client.send_message(
        settings.target_channel_id,
        text,
        parse_mode="html",
    )
    print(f"Sent to {settings.target_channel_id}")
    await client.disconnect()

asyncio.run(main())
