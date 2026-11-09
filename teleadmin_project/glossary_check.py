"""Live regression check for the FPL glossary.

Translates a set of sentences that have gone wrong before and checks each
result for the Persian it must contain and the Persian it must not. Run it
after changing glossary_prompt.txt, tone_prompt.txt or the model:

    cd teleadmin_project && .venv/bin/python glossary_check.py

It calls the configured providers, so it uses real quota (one call per case).
"""
import asyncio
import sys

import config
from translator import Translator

# (English source, must contain any of, must not contain any of)
CASES = [
    ("Saka has 6 goal contributions in his last 4 games.", ["مشارکت در گل"], ["امتیاز"]),
    ("Palmer blanked again at the weekend.", ["پاس گل"], ["امتیاز"]),
    ("He's returned in every home game this season.", ["گل یا پاس گل", "مشارکت در گل"], ["امتیاز"]),
    ("Gordon has 9 goal involvements, more than any other midfielder.", ["مشارکت در گل"], ["امتیاز"]),
    ("Haaland hauled with 18 points against Wolves.", ["امتیاز"], []),
    ("Mbeumo has 8 attacking returns from his last 6.", ["مشارکت در گل", "گل یا پاس گل"], ["امتیاز"]),
    ("Is it worth taking a -4 hit to bring in Isak?", ["منفی"], []),
    ("I'm rolling my transfer and keeping £1.5m in the bank.", ["£1.5m"], ["میلیون پوند"]),
    ("Arsenal have a Blank Gameweek in GW31.", ["هفته پوچ"], []),
    ("Fantasy Football Scout's Bus Team for GW12.", ["اسکات", "تیم حداقلی"], []),
    ("He's on penalties and set pieces, so he's nailed for points.", ["پنالتی", "فیکس"], []),
    ("Our template team has a triple-up on Arsenal defence.", ["تمپلیت"], []),
]


async def main() -> int:
    cfg = config.load_config()
    tr = Translator(
        cfg.openrouter_api_key, cfg.openrouter_model, cfg.fallback_model,
        google_api_key=cfg.google_aistudio_key, google_model=cfg.google_aistudio_model,
    )
    failures = 0
    for source, required, forbidden in CASES:
        out = await tr.translate(source)
        missing = not any(term in out for term in required)
        banned = [term for term in forbidden if term in out]
        ok = not missing and not banned
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {source}\n      {out}")
        if missing:
            print(f"      missing one of: {required}")
        if banned:
            print(f"      contains: {banned}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
