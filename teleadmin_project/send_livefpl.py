"""Post LiveFPL player points as text to Telegram — test/utility."""
import asyncio
import os
import sys

from config import load_config
from telethon import TelegramClient
from telethon.sessions import StringSession
import livefpl

settings = load_config()

_session_string = os.getenv("TELETHON_SESSION_STRING")
client = TelegramClient(
    StringSession(_session_string) if _session_string else "translation_session",
    settings.telegram_api_id,
    settings.telegram_api_hash,
)


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        fixtures = livefpl.get_finished_fixtures()
    else:
        fixtures = livefpl.get_finished_fixtures(gameweek_id=38)

    if not fixtures:
        print("No finished fixtures found")
        return

    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
    fixtures = fixtures[:limit] if limit > 0 else fixtures

    print(f"Processing {len(fixtures)} fixtures via API...")
    await client.start()

    for fix in fixtures:
        home = fix["home_en"]
        away = fix["away_en"]
        print(f"\nProcessing: {home} vs {away}")

        text = livefpl.build_game_text(fix)
        if not text:
            print(f"  Failed")
            continue

        await client.send_message(
            settings.target_channel_id,
            text,
            parse_mode="html",
        )
        print(f"  Posted ({len(text)} chars)")

    await client.disconnect()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
