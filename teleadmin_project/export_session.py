"""Create a fresh Telegram login for deployment and print it as a session string.

    cd teleadmin_project && .venv/bin/python export_session.py

It asks for the phone number and the login code, then prints a
TELETHON_SESSION_STRING to paste into Coolify. The login is made in memory
only: it never touches the local .session file, so the deployed bot and a
local run never share a login (Telegram invalidates a login used from two
places with AuthKeyDuplicatedError), and nothing is written that could be
committed. Run it in your own terminal, so the string is never logged or
pasted anywhere but Coolify.

The bot is not started; only the login is made.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import load_config


async def main() -> None:
    settings = load_config()
    client = TelegramClient(StringSession(), settings.telegram_api_id, settings.telegram_api_hash)
    await client.start()
    me = await client.get_me()
    string = client.session.save()
    await client.disconnect()
    print(f"\nLogged in as {me.first_name} (id {me.id}). New TELETHON_SESSION_STRING:\n")
    print(string)
    print("\nPaste it into Coolify, redeploy, then end the old bot session in Telegram.")


if __name__ == "__main__":
    asyncio.run(main())
