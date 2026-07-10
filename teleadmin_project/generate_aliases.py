"""Batch-generate FPL community aliases for popular players via OpenRouter."""
import asyncio
import json
import logging
import sys

from openai import AsyncOpenAI

from config import load_config
from database import _connect, query_scalar

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
DELAY_BETWEEN_BATCHES = 3

ALIAS_PROMPT = """You are an expert in Fantasy Premier League (FPL) community culture.
For each player below, provide their commonly used FPL community alias/abbreviation if one is widely known.
If the player doesn't have a widely recognized alias, return null.

Examples of aliases:
- "VVD" for Virgil van Dijk
- "TAA" for Trent Alexander-Arnold
- "KDB" for Kevin De Bruyne
- "MLS" for Myles Lewis-Skelly
- "Jota" for Diogo Jota (when web_name is already the alias, return null)

Rules:
- Only include aliases that are genuinely used by the FPL community (Twitter, Reddit, forums).
- Do NOT fabricate aliases. If unsure, return null.
- Output ONLY a JSON array of objects. No markdown, no explanation.
- Do NOT include the player's web_name as an alias — it must be different.

Input:
{input_json}

Output format — a JSON array of objects, each with "id" and "alias" (string or null):
"""


async def generate_aliases(
    client: AsyncOpenAI, model: str, players: list[dict], fallback_model: str
) -> list[dict]:
    input_json = json.dumps(
        [
            {"id": p["id"], "web_name": p["web_name"], "first_name": p["first_name"],
             "second_name": p["second_name"]}
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
                    "content": ALIAS_PROMPT.format(input_json=input_json),
                }
            ],
            temperature=0.2,
            max_tokens=4096,
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("\n```", 1)[0]
        return json.loads(text)

    try:
        return await call(model)
    except Exception as e:
        logger.warning("Primary model failed: %s. Trying fallback.", e)
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

    total = query_scalar(
        "SELECT count(*) FROM players WHERE alias IS NULL AND total_points > 0"
    )
    if total == 0:
        logger.info("All players already have aliases. Nothing to do.")
        return

    limit = min(200, total)
    logger.info("Generating aliases for up to %d players", limit)

    with _connect() as conn:
        conn.row_factory = lambda c, r: dict(
            zip([col[0] for col in c.description], r)
        )
        rows = conn.execute(
            "SELECT id, first_name, second_name, web_name FROM players "
            "WHERE alias IS NULL AND total_points > 0 "
            "ORDER BY selected_by_percent DESC LIMIT ?",
            (limit,),
        ).fetchall()

    if not rows:
        logger.info("No players to process.")
        return

    updated = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        logger.info(
            "Batch %d: players %d-%d (%d names)",
            i // BATCH_SIZE + 1,
            batch[0]["id"],
            batch[-1]["id"],
            len(batch),
        )

        try:
            result = await generate_aliases(
                client, settings.openrouter_model, batch, settings.fallback_model
            )
        except Exception as e:
            logger.error("Failed to generate aliases for batch: %s", e)
            sys.exit(1)

        result_by_id = {item["id"]: item.get("alias") for item in result}

        with _connect() as conn:
            for player in batch:
                pid = player["id"]
                alias = result_by_id.get(pid)
                if alias:
                    conn.execute(
                        "UPDATE players SET alias=? WHERE id=?", (alias, pid)
                    )
                    updated += 1

        logger.info("Assigned %d aliases in this batch", 
                    sum(1 for v in result_by_id.values() if v))
        await asyncio.sleep(DELAY_BETWEEN_BATCHES)

    logger.info("Done. Assigned %d aliases total.", updated)


if __name__ == "__main__":
    asyncio.run(main())
