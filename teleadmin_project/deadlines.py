"""Automate deadline-passed posts (event-driven)."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database as db

logger = logging.getLogger(__name__)

_DEADLINE_IMAGE = Path(__file__).parent / "deadline.jpg"


def deadline_passed_text(gw_id: int, league_code: str) -> str:
    link = f"https://fantasy.premierleague.com/leagues/auto-join/{league_code}"
    return (
        f"❌ ددلاین هفته <b>{gw_id}</b> سپری شد. ❌\n\n"
        f"برای همه تون فلش سبز آرزومندیم.\n\n"
        f'عضویت در لیگ ما <a href="{link}"><b>({league_code})</b></a>\n\n'
        f"@EPL_Fantasy"
    )


async def run_deadline_loop(client, target_channel: str, league_code: str):
    logger.info("Deadline loop started")

    while True:
        now = datetime.now(tz=timezone.utc)

        gw = db.query_one(
            "SELECT id, deadline_time FROM gameweeks "
            "WHERE is_next = 1 OR is_current = 1 "
            "ORDER BY id LIMIT 1"
        )
        if not gw:
            logger.info("No upcoming gameweek found, sleeping 1h")
            await asyncio.sleep(3600)
            continue

        gw_id = gw["id"]
        deadline_utc = datetime.strptime(
            gw["deadline_time"][:19], "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=timezone.utc)

        if now >= deadline_utc:
            await _post_deadline_passed(client, target_channel, gw, league_code)
            _advance_gw(gw_id)
            continue

        wait = (deadline_utc - now).total_seconds()
        logger.info(
            "Sleeping %.0fs until GW%d deadline at %s",
            wait, gw_id, deadline_utc.isoformat(),
        )
        await asyncio.sleep(max(1, wait))

        await _post_deadline_passed(client, target_channel, gw, league_code)
        _advance_gw(gw_id)


async def _post_deadline_passed(client, target_channel, gw: dict, league_code: str):
    key = f"deadline_post_{gw['id']}"
    already_posted = db.query_scalar(
        "SELECT value FROM last_updated WHERE key = ?", (key,)
    )
    if already_posted:
        logger.info("GW%d deadline already posted, skipping", gw["id"])
        return

    text = deadline_passed_text(gw["id"], league_code)
    logger.info("Posting deadline-passed for GW%d", gw["id"])
    await client.send_file(
        target_channel,
        str(_DEADLINE_IMAGE),
        caption=text,
        parse_mode="html",
    )
    with db._connect() as conn:
        db._set_updated(conn, key)
    logger.info("Posted deadline-passed for GW%d", gw["id"])


def _advance_gw(gw_id: int):
    with db._connect() as conn:
        conn.execute(
            "UPDATE gameweeks SET is_current=0, is_next=0 WHERE id=?", (gw_id,)
        )
        nxt = db.query_one("SELECT id FROM gameweeks WHERE id=?", (gw_id + 1,))
        if nxt:
            conn.execute(
                "UPDATE gameweeks SET is_next=1 WHERE id=?", (gw_id + 1,)
            )
        conn.commit()
    logger.info("Advanced to GW%d", gw_id + 1)
