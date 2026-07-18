"""Scheduler for automated posts: price predictions, EO leaderboard, game points."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import database as db
import livefpl

logger = logging.getLogger(__name__)

_IRAN_OFFSET = timedelta(hours=3, minutes=30)

_PRICE_POSTED_KEY = "price_prediction_posted"
_EO_POSTED_KEY = "eo_posted"


def _now_iran() -> datetime:
    return datetime.now(tz=timezone.utc) + _IRAN_OFFSET


async def run_scheduler(client, target_channel: str, league_code: str, price_predictions_enabled: bool = True):
    logger.info("Scheduler started")
    await asyncio.sleep(5)

    while True:
        try:
            now_iran = _now_iran()

            games = livefpl._fetch_games() if livefpl._games_cache is None else None
            if games is None:
                livefpl._games_cache = livefpl._fetch_games()
                games = livefpl._games_cache

            await _check_game_points(client, target_channel, games)

            await _check_eo_post(client, target_channel)

            if price_predictions_enabled:
                await _check_price_post(client, target_channel, games, now_iran)

        except Exception as e:
            logger.error("Scheduler error: %s", e)

        await asyncio.sleep(60)


async def _check_game_points(client, target_channel, games):
    fixtures = livefpl.get_finished_fixtures()
    fixture_map = {(f["home_en"], f["away_en"]): f for f in fixtures}

    for g in games:
        if g[4] != "Done":
            continue

        home_en = g[0]
        away_en = g[1]
        key = (home_en, away_en)
        fixture = fixture_map.get(key)
        if not fixture:
            continue

        # Skip if already posted
        posted_key = f"game_points_{fixture['id']}"
        if _already_posted(posted_key):
            continue

        text = livefpl.build_game_text(fixture)
        if not text:
            continue

        await client.send_message(target_channel, text, parse_mode="html")
        _mark_posted(posted_key)
        logger.info("Posted game points for %s vs %s", home_en, away_en)
        await asyncio.sleep(2)


async def _check_eo_post(client, target_channel):
    if _already_posted(_EO_POSTED_KEY):
        return

    # Get the latest deadline that has passed
    gw = db.query_one(
        "SELECT id, deadline_time FROM gameweeks WHERE finished=1 OR is_current=1 ORDER BY id DESC LIMIT 1"
    )
    if not gw:
        return

    deadline_utc = datetime.strptime(
        gw["deadline_time"][:19], "%Y-%m-%dT%H:%M:%S"
    ).replace(tzinfo=timezone.utc)

    post_time = deadline_utc + timedelta(minutes=75)
    now_utc = datetime.now(tz=timezone.utc)

    if now_utc < post_time:
        return

    text = livefpl.build_eo_text()
    if not text:
        return

    await client.send_message(target_channel, text, parse_mode="html")
    _mark_posted(_EO_POSTED_KEY)
    logger.info("Posted EO leaderboard for GW%d", gw["id"])


async def _check_price_post(client, target_channel, games, now_iran):
    # Only post once per day
    today_key = _now_iran().strftime("%Y-%m-%d")
    key = f"{_PRICE_POSTED_KEY}_{today_key}"
    if _already_posted(key):
        return

    # Check if any game is currently live
    live_active = any(g[4] == "Live" or g[4] == "Playing" for g in games)

    if live_active:
        return  # Wait until games finish

    # Check if all games are done (post 30 min after last live game)
    all_done = all(g[4] == "Done" for g in games) if games else False

    # Post at 23:30 Iran time
    target_hour = 23
    target_min = 30

    if now_iran.hour < target_hour or (now_iran.hour == target_hour and now_iran.minute < target_min):
        return

    # If this is past 23:30 but some games were live earlier,
    # post 30 min after they all finished
    if all_done:
        # Games are all done - post immediately if past 23:30
        text = livefpl.build_price_changes_text()
        if text:
            await client.send_message(target_channel, text, parse_mode="html")
            _mark_posted(key)
            logger.info("Posted price change predictions")
    else:
        # No games at all - post at 23:30
        text = livefpl.build_price_changes_text()
        if text:
            await client.send_message(target_channel, text, parse_mode="html")
            _mark_posted(key)
            logger.info("Posted price change predictions (no live games)")


def _already_posted(key: str) -> bool:
    val = db.query_scalar("SELECT value FROM last_updated WHERE key = ?", (key,))
    return val is not None


def _mark_posted(key: str) -> None:
    with db._connect() as conn:
        db._set_updated(conn, key)
