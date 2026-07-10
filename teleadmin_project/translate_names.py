"""Batch-translate player names to Farsi via OpenRouter and update the DB."""
import asyncio
import json
import logging
import sys
import textwrap

from openai import AsyncOpenAI

from config import load_config
from database import _connect, query_scalar

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
                    "content": NAME_TRANSLATION_PROMPT.format(input_json=input_json),
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
        if expected_ids != returned_ids:
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


async def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    settings = load_config()
    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "TeleAdmin"},
    )

    total = query_scalar("SELECT count(*) FROM players")
    logger.info("Re-translating names for %d players (batch size: %d)", total, BATCH_SIZE)

    translated = 0
    offset = 0

    while True:
        with _connect() as conn:
            conn.row_factory = lambda c, r: dict(
                zip([col[0] for col in c.description], r)
            )
            rows = conn.execute(
                "SELECT id, first_name, second_name, web_name FROM players "
                "ORDER BY id LIMIT ? OFFSET ?",
                (BATCH_SIZE, offset),
            ).fetchall()

        if not rows:
            break

        logger.info(
            "Batch %d: players %d-%d (%d names)",
            offset // BATCH_SIZE + 1,
            rows[0]["id"],
            rows[-1]["id"],
            len(rows),
        )

        try:
            result = await translate_batch(
                client, settings.openrouter_model, rows, settings.fallback_model
            )
        except Exception as e:
            logger.error("Failed to translate batch: %s", e)
            sys.exit(1)

        result_by_id = {item["id"]: item for item in result}

        with _connect() as conn:
            for player in rows:
                pid = player["id"]
                tr = result_by_id.get(pid, {})
                conn.execute(
                    "UPDATE players SET first_name_fa=?, second_name_fa=?, web_name_fa=? WHERE id=?",
                    (
                        tr.get("first_name_fa", ""),
                        tr.get("second_name_fa", ""),
                        tr.get("web_name_fa", ""),
                        pid,
                    ),
                )

        translated += len(rows)
        offset += len(rows)
        logger.info(
            "Updated %d players in DB (%d/%d complete)",
            len(rows),
            translated,
            total,
        )

        if len(rows) < BATCH_SIZE:
            break

        await asyncio.sleep(DELAY_BETWEEN_BATCHES)

    logger.info("Done. Translated %d players.", translated)


if __name__ == "__main__":
    asyncio.run(main())
