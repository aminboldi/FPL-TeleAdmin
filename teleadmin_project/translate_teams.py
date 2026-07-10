"""Translate team names to Farsi and update the database."""
import asyncio
import json
import logging
from openai import AsyncOpenAI
from config import load_config
from database import _connect

logging.basicConfig(level=logging.INFO)

async def main():
    settings = load_config()
    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "TeleAdmin"},
    )

    with _connect() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        teams = conn.execute("SELECT id, name FROM teams ORDER BY id").fetchall()

    prompt = (
        "Translate these English Premier League club names into Persian (Farsi). "
        "For each club provide name_fa (full Farsi name) and short_name_fa (short Farsi name, 1-3 words).\n\n"
        "Input: "
        + json.dumps([{"id": t["id"], "name": t["name"]} for t in teams], ensure_ascii=False)
        + '\n\nOutput ONLY a JSON array: [{"id": 1, "name_fa": "...", "short_name_fa": "..."}, ...]'
    )

    response = await client.chat.completions.create(
        model=settings.openrouter_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=2048,
    )
    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("\n```", 1)[0]
    result = json.loads(text)

    by_id = {r["id"]: r for r in result}
    with _connect() as conn:
        for t in teams:
            tr = by_id.get(t["id"], {})
            conn.execute(
                "UPDATE teams SET name_fa=?, short_name_fa=? WHERE id=?",
                (tr.get("name_fa", ""), tr.get("short_name_fa", ""), t["id"]),
            )
        conn.commit()

    for t in teams:
        tr = by_id.get(t["id"], {})
        print(f'{t["name"]:25s} -> {tr.get("name_fa", "?")}  ({tr.get("short_name_fa", "?")})')

    print("\nDone.")

asyncio.run(main())
