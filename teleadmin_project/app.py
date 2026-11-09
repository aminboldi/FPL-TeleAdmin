"""Process-wide runtime objects: settings, the translator and the Telegram user client.

Importing this module loads the configuration, initialises both databases
and builds the clients, so pure helpers must not import it."""
import logging
import os
import sys


from telethon import TelegramClient
from telethon.sessions import StringSession

from config import load_config
from translator import Translator
import database as db
import runtime_config


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)


logger = logging.getLogger("TeleAdmin")


settings = load_config()


runtime_config.init()


# Ensure the SQLite schema exists before any handler or scheduler task
# touches the database.
db.init_db()


translator = Translator(
    api_key=settings.openrouter_api_key,
    model=settings.openrouter_model,
    fallback_model=settings.fallback_model,
    google_api_key=settings.google_aistudio_key,
    google_model=settings.google_aistudio_model,
)


_session_string = os.getenv("TELETHON_SESSION_STRING")


client = TelegramClient(
    StringSession(_session_string) if _session_string else "translation_session",
    settings.telegram_api_id,
    settings.telegram_api_hash,
)


QUEUED_POST_CONFIRMATION = "در اولین بازهٔ خالی زمان‌بندی کانال قرار گرفت."


def _target_channel() -> str:
    return runtime_config.get("TARGET_CHANNEL_ID")


def _refresh_translator_model() -> None:
    translator.openrouter_model = runtime_config.get("OPEN_ROUTER_MODEL")
