"""Export the local Telethon session as a string session for Render deployment."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from telethon import TelegramClient
from telethon.sessions import StringSession

settings = load_config()

client = TelegramClient(
    "translation_session",  # reads the local .session file
    settings.telegram_api_id,
    settings.telegram_api_hash,
)

async def main():
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: not authorized. Run 'python bot.py' first to log in.")
        await client.disconnect()
        return

    string = StringSession.save(client.session)
    print(string)
    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
