import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)


@dataclass
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    openrouter_api_key: str
    openrouter_model: str
    fallback_model: str
    source_channel_id: str
    source_channel2_id: str | None
    target_channel_id: str
    notif_channel_id: str | None


def load_config() -> Settings:
    required = {
        "TELEGRAM_API_ID": int,
        "TELEGRAM_API_HASH": str,
        "OPEN_ROUTER_API_KEY": str,
        "SOURCE_CHANNEL_ID": str,
        "TARGET_CHANNEL_ID": str,
    }

    missing = []
    for key in required:
        if not os.getenv(key):
            missing.append(key)

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Please set them in the .env file at {env_path}"
        )

    return Settings(
        telegram_api_id=int(os.getenv("TELEGRAM_API_ID")),
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH"),
        openrouter_api_key=os.getenv("OPEN_ROUTER_API_KEY"),
        openrouter_model=os.getenv(
            "OPEN_ROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"
        ),
        fallback_model="google/gemini-2.5-flash-lite",
        source_channel_id=os.getenv("SOURCE_CHANNEL_ID"),
        source_channel2_id=os.getenv("SOURCE_CHANNEL2_ID") or None,
        target_channel_id=os.getenv("TARGET_CHANNEL_ID"),
        notif_channel_id=os.getenv("NOTIF_CHANNEL_ID") or None,
    )
