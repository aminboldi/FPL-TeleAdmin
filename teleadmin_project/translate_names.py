"""Batch-translate player names to Farsi via OpenRouter and update the DB."""
import asyncio
import json
import logging
import re

from openai import AsyncOpenAI

from config import load_config
from database import _connect

logger = logging.getLogger(__name__)

BATCH_SIZE = 40
DELAY_BETWEEN_BATCHES = 3  # seconds to respect free-tier rate limits

NAME_TRANSLATION_PROMPT = """You are translating English football player names into Persian (Farsi).
There are three name fields per player: first_name, second_name (last name), and web_name (display name used in FPL).

Rules:
- Transliterate phonetically into Persian script — NOT a literal translation.
- Preserve the original pronunciation as closely as possible.
- web_name should be a natural short Persian rendering (like how Iranian commentators say it).
- Output ONLY a JSON array of objects. No markdown, no explanation.

Input format — a JSON array of objects with keys "id", "first_name", "second_name", "web_name":
{input_json}

Output format — a JSON array of objects with keys "id", "first_name_fa", "second_name_fa", "web_name_fa":
"""


async def translate_batch(
    client: AsyncOpenAI, model: str, players: list[dict], fallback_model: str
) -> list[dict]:
    input_json = json.dumps(
        [
            {
                "id": p["id"],
                "first_name": p["first_name"],
                "second_name": p["second_name"],
                "web_name": p["web_name"],
            }
            for p in players
        ],
        ensure_ascii=False,
    )

    async def call(m: str) -> list[dict]:
        response = await client.chat.completions.create(
            model=m,
            messages=[
                {
                    "role": "user",
                    "content": NAME_TRANSLATION_PROMPT.replace("{input_json}", input_json),
                }
            ],
            temperature=0.1,
            max_tokens=4096,
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("\n```", 1)[0]
        result = json.loads(text)
        if not isinstance(result, list):
            raise ValueError(f"Expected list, got {type(result)}")
        expected_ids = {p["id"] for p in players}
        returned_ids = {item["id"] for item in result}
        if expected_ids != returned_ids or len(result) != len(players):
            raise ValueError(
                f"ID mismatch: expected {len(expected_ids)} IDs, "
                f"got {len(returned_ids)}. "
                f"Missing: {sorted(expected_ids - returned_ids)[:5]}, "
                f"Extra: {sorted(returned_ids - expected_ids)[:5]}"
            )
        return result

    for attempt in range(3):
        try:
            return await call(model)
        except Exception as e:
            logger.warning("Primary model attempt %d failed: %s", attempt + 1, e)
    try:
        return await call(fallback_model)
    except Exception as e2:
        logger.error("Fallback also failed: %s", e2)
        raise


async def fill_missing_names(client: AsyncOpenAI, model: str, fallback_model: str) -> int:
    """Fill missing Persian fields, preserving every existing spelling and alias.

    Scan all incomplete rows, including additions from earlier refreshes whose
    translation failed. Recheck each field at write time so a concurrent admin
    edit made during the API call also wins.
    """
    with _connect() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        rows = conn.execute(
            "SELECT id, first_name, second_name, web_name FROM players "
            "WHERE trim(coalesce(first_name_fa, '')) = '' "
            "OR trim(coalesce(second_name_fa, '')) = '' "
            "OR trim(coalesce(web_name_fa, '')) = '' ORDER BY id"
        ).fetchall()
    updated = 0
    for offset in range(0, len(rows), BATCH_SIZE):
        batch = rows[offset:offset + BATCH_SIZE]
        result = await translate_batch(client, model, batch, fallback_model)
        updates = []
        for item in result:
            names = []
            for field in ("first_name_fa", "second_name_fa", "web_name_fa"):
                value = item.get(field)
                if not isinstance(value, str) or not re.search(r"[\u0600-\u06ff]", value):
                    raise ValueError(f"Missing Persian {field} for player {item['id']}")
                names.append(value.strip())
            updates.append((*names, item["id"]))
        with _connect() as conn:
            cursor = conn.executemany(
                "UPDATE players SET "
                "first_name_fa=CASE WHEN trim(coalesce(first_name_fa, ''))='' THEN ? ELSE first_name_fa END, "
                "second_name_fa=CASE WHEN trim(coalesce(second_name_fa, ''))='' THEN ? ELSE second_name_fa END, "
                "web_name_fa=CASE WHEN trim(coalesce(web_name_fa, ''))='' THEN ? ELSE web_name_fa END "
                "WHERE id=? AND (trim(coalesce(first_name_fa, ''))='' "
                "OR trim(coalesce(second_name_fa, ''))='' OR trim(coalesce(web_name_fa, ''))='')",
                updates,
            )
            updated += cursor.rowcount
        if offset + BATCH_SIZE < len(rows):
            await asyncio.sleep(DELAY_BETWEEN_BATCHES)
    return updated


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    settings = load_config()
    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "TeleAdmin"},
    )
    updated = await fill_missing_names(client, settings.openrouter_model, settings.fallback_model)
    logger.info("Filled missing Persian names for %d players", updated)


if __name__ == "__main__":
    asyncio.run(main())
