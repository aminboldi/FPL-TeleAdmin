"""Scheduler for automated posts: price predictions, EO leaderboard, game points."""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import database as db
import livefpl
import runtime_config
import alerts
import official_lineups

logger = logging.getLogger(__name__)

_IRAN_OFFSET = timedelta(hours=3, minutes=30)

_PRICE_POSTED_KEY = "price_prediction_posted"
_EO_POSTED_KEY = "eo_posted"
_PRICE_CHANGES_RESUME_DATE = date(2026, 8, 21)
_DB_REFRESH_INTERVAL = 6 * 60 * 60
_DB_REFRESH_RETRY_INTERVAL = 30 * 60
_SCHEDULER_INTERVAL = 30
_LINEUP_LEAD_TIME = timedelta(minutes=75)


def _now_iran() -> datetime:
    return datetime.now(tz=timezone.utc) + _IRAN_OFFSET


async def run_scheduler(client, target_channel: str, league_code: str, price_predictions_enabled: bool = True):
    logger.info("Scheduler started")
    await asyncio.sleep(5)
    next_db_refresh = 0.0

    while True:
        try:
            now_monotonic = asyncio.get_running_loop().time()
            if now_monotonic >= next_db_refresh:
                try:
                    result = await asyncio.to_thread(db.refresh_from_fpl_api)
                    logger.info(
                        "FPL database refreshed: %d players, %d fixtures, %d new players",
                        result["player_count"],
                        result["fixture_count"],
                        len(result["new_players"]),
                    )
                    next_db_refresh = now_monotonic + _DB_REFRESH_INTERVAL
                except Exception as exc:
                    logger.warning("FPL database refresh failed: %s", exc)
                    next_db_refresh = now_monotonic + _DB_REFRESH_RETRY_INTERVAL

            now_iran = _now_iran()
            target_channel = runtime_config.get("TARGET_CHANNEL_ID") or target_channel
            price_predictions_enabled = runtime_config.get_bool("PRICE_PREDICTIONS_ENABLED")
            if not target_channel:
                await asyncio.sleep(_SCHEDULER_INTERVAL)
                continue

            # Official lineups normally become available 75 minutes before
            # kickoff.  Once that window opens, this job retries every 30s
            # until both starting XIs are present and successfully posted.
            await _check_official_lineups(client, target_channel)

            # LiveFPL changes as matches progress.  A one-time startup fetch
            # leaves all games permanently at their initial status and makes
            # both Done-game posts and the post-deadline EO snapshot invisible.
            games = await asyncio.to_thread(livefpl.refresh_games)
            if not games:
                await asyncio.sleep(_SCHEDULER_INTERVAL)
                continue

            await _check_game_points(client, target_channel, games)

            await _check_eo_post(client, target_channel)

            if price_predictions_enabled:
                await _check_price_post(client, target_channel, games, now_iran)

        except Exception as e:
            logger.error("Scheduler error: %s", e)

        await asyncio.sleep(_SCHEDULER_INTERVAL)


async def _check_official_lineups(client, target_channel):
    now_utc = datetime.now(tz=timezone.utc)
    fixtures = livefpl.get_fixtures()

    for fixture in fixtures:
        posted_key = f"official_lineup_{fixture['id']}"
        if _already_posted(posted_key):
            continue
        if fixture.get("finished"):
            continue

        try:
            kickoff = datetime.strptime(
                fixture["kickoff_time"][:19], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue

        # Poll from 75 minutes before kickoff until the fixture is finished.
        # This keeps retrying through a transient outage at kickoff without
        # retrying old completed fixtures forever.
        if now_utc < kickoff - _LINEUP_LEAD_TIME:
            continue

        try:
            parsed = await asyncio.to_thread(
                official_lineups.fetch_starting_lineups, fixture
            )
        except Exception as exc:
            logger.warning(
                "Official lineup fetch failed for %s vs %s: %s",
                fixture["home_en"], fixture["away_en"], exc,
            )
            continue

        if not parsed:
            logger.info(
                "Official lineups not ready for %s vs %s; retrying in %ss",
                fixture["home_en"], fixture["away_en"], _SCHEDULER_INTERVAL,
            )
            continue

        text = alerts.format_lineup(parsed)
        if not text:
            logger.warning(
                "Official lineup formatting returned no text for %s vs %s",
                fixture["home_en"], fixture["away_en"],
            )
            continue

        lineup_key = alerts.lineup_dedup_key(parsed, fixture["kickoff_time"][:10])
        if _already_posted(lineup_key):
            _mark_posted(posted_key)
            logger.info(
                "Skipping official lineup already posted by a source for %s vs %s",
                fixture["home_en"], fixture["away_en"],
            )
            continue

        await client.send_message(target_channel, text, parse_mode="html")
        _mark_posted(posted_key)
        _mark_posted(lineup_key)
        logger.info(
            "Posted official lineups for %s vs %s",
            fixture["home_en"], fixture["away_en"],
        )
        await asyncio.sleep(2)


async def _check_game_points(client, target_channel, games):
    # LiveFPL is the authoritative source for the current match status.  The
    # official FPL API can leave `finished=false` while bonus/stat processing
    # is still provisional, so filtering the DB here drops valid Done games.
    fixtures = livefpl.get_fixtures()
    fixture_map = {(f["home_en"], f["away_en"]): f for f in fixtures}

    for g in games:
        if not livefpl.is_game_finished(g):
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
    # Get the latest deadline that has passed
    gw = db.query_one(
        "SELECT id, deadline_time FROM gameweeks "
        "WHERE datetime(deadline_time) <= datetime('now') "
        "ORDER BY id DESC LIMIT 1"
    )
    if not gw:
        return

    posted_key = f"{_EO_POSTED_KEY}_{gw['id']}"
    if _already_posted(posted_key):
        return

    deadline_utc = datetime.strptime(
        gw["deadline_time"][:19], "%Y-%m-%dT%H:%M:%S"
    ).replace(tzinfo=timezone.utc)

    post_time = deadline_utc + timedelta(minutes=75)
    now_utc = datetime.now(tz=timezone.utc)

    if now_utc < post_time:
        return

    text = livefpl.build_eo_text(gameweek_id=gw["id"])
    if not text:
        return

    await client.send_message(target_channel, text, parse_mode="html")
    _mark_posted(posted_key)
    logger.info("Posted EO leaderboard for GW%d", gw["id"])


async def _check_price_post(client, target_channel, games, now_iran):
    if now_iran.date() < _PRICE_CHANGES_RESUME_DATE:
        return

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
