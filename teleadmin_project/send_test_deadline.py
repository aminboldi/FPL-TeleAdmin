"""Send a test deadline-passed post to the target channel."""
import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import load_config

async def main():
    settings = load_config()
    session_str = os.getenv("TELETHON_SESSION_STRING")
    client = TelegramClient(
        StringSession(session_str) if session_str else "translation_session",
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )

    await client.start()
    print("Connected.")

    link = f"https://fantasy.premierleague.com/leagues/auto-join/{settings.league_code}"
    text = (
        f"❌ ددلاین هفته 39 سپری شد. ❌\n\n"
        f"برای همه تون فلش سبز آرزومندیم.\n\n"
        f'عضویت در لیگ ما <a href="{link}">({settings.league_code})</a>\n\n'
        f"@EPL_Fantasy"
    )

    import sys, pathlib
    img = pathlib.Path(__file__).parent / "deadline.jpg"

    await client.send_file(
        settings.target_channel_id,
        str(img),
        caption=text,
        parse_mode="html",
    )
    print(f"Sent deadline-passed test to {settings.target_channel_id}")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
