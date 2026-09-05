"""Scheduler for automated posts: price changes, EO leaderboard, game points."""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import database as db
import livefpl
import runtime_config
import alerts
import official_lineups
import player_names

logger = logging.getLogger(__name__)

_IRAN_OFFSET = timedelta(hours=3, minutes=30)

_PRICE_POSTED_KEY = "price_prediction_posted"
_EO_POSTED_KEY = "eo_posted"
_PRICE_CHANGES_RESUME_DATE = date(2026, 8, 21)
# Iran-time minutes past midnight. FPL applies the change at about 05:00 Iran
# time, so the watchlist stays postable until 03:00 rather than expiring at
# midnight thirty minutes after it became due.
_PRICE_PREDICTION_OPENS = 23 * 60 + 30
_PRICE_PREDICTION_CLOSES = 3 * 60
# The bootstrap payload is ~1.7MB, so poll for price movement on its own
# cadence rather than on every 30s scheduler tick.
_PRICE_POLL_INTERVAL = 5 * 60
_price_check_due_at = 0.0
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
                    # A refresh can add players, and translations resolve
                    # Persian names through a cached index of them.
                    await asyncio.to_thread(player_names.reload)
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

            # Each job is isolated: a failure in one must not starve the
            # others for the rest of the process lifetime. Sharing a single
            # try block previously let a lineup error silently suppress every
            # price report.
            #
            # Official lineups normally become available 75 minutes before
            # kickoff.  Once that window opens, this job retries every 30s
            # until both starting XIs are present and successfully posted.
            try:
                await _check_official_lineups(client, target_channel)
            except Exception:
                logger.exception("Official lineup check failed")

            # Price reports use the official FPL bootstrap and must not be
            # blocked by a temporary outage or empty response from LiveFPL.
            try:
                await _check_price_post(
                    client, target_channel, now_iran, price_predictions_enabled
                )
            except Exception:
                logger.exception("Price report check failed")

            # LiveFPL changes as matches progress. A one-time startup fetch
            # leaves all games permanently at their initial status and makes
            # both Done-game posts and the post-deadline EO snapshot invisible.
            games = await asyncio.to_thread(livefpl.refresh_games)
            if games:
                try:
                    await _check_game_points(client, target_channel, games)
                except Exception:
                    logger.exception("Game points check failed")
                try:
                    await _check_eo_post(client, target_channel)
                except Exception:
                    logger.exception("EO leaderboard check failed")

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


async def _check_price_post(client, target_channel, now_iran, price_predictions_enabled=True):
    """Report real price changes when they happen, and predictions at 23:30.

    Actual changes are detected by diffing the official prices against the
    saved baseline rather than by posting at a fixed clock time.  FPL applies
    changes at roughly 01:30 UTC, but the exact moment drifts, so a fixed-time
    post either fires before the change has landed or misses it entirely.
    Diffing is self-correcting: it also catches changes missed while the bot
    was down, and it cannot fire twice for the same change because the
    baseline advances only after a successful post.
    """
    if now_iran.date() < _PRICE_CHANGES_RESUME_DATE:
        return

    # Isolated from each other for the same reason the scheduler isolates its
    # jobs: a failure fetching or posting confirmed changes must not also cost
    # the night's watchlist.
    try:
        await _check_actual_price_changes(client, target_channel)
    except Exception:
        logger.exception("Confirmed price-change check failed")

    if not price_predictions_enabled:
        return
    prediction_key = _price_prediction_key(now_iran)
    if prediction_key is None or _already_posted(prediction_key):
        return
    text = await asyncio.to_thread(
        livefpl.build_price_changes_text,
        include_actual=False,
        include_potential=True,
    )
    if not text:
        logger.warning("Price prediction watchlist produced no text; will retry")
        return
    await client.send_message(target_channel, text, parse_mode="html")
    _mark_posted(prediction_key)
    logger.info("Posted price prediction watchlist for %s", prediction_key)


def _price_prediction_key(now_iran) -> str | None:
    """Return the key for tonight's watchlist, or None outside its window.

    The window opens at 23:30 and stays open until 03:00, because the change
    itself lands around 05:00 and the watchlist is worth posting right up to
    it. It used to close at midnight, which left half an hour in which a
    restart, a deploy, or a slow tick lost the night's post entirely — and
    silently, because the next day's key is a different one.

    The key names the date of the *change*, not of the moment, so it stays the
    same on either side of midnight and the post cannot repeat.
    """
    minutes = now_iran.hour * 60 + now_iran.minute
    if minutes >= _PRICE_PREDICTION_OPENS:
        change_date = now_iran.date() + timedelta(days=1)
    elif minutes < _PRICE_PREDICTION_CLOSES:
        change_date = now_iran.date()
    else:
        return None
    return f"{_PRICE_POSTED_KEY}_{change_date.isoformat()}"


async def _check_actual_price_changes(client, target_channel) -> bool:
    """Post confirmed price changes as soon as the official prices move."""
    global _price_check_due_at
    loop_now = asyncio.get_running_loop().time()
    if loop_now < _price_check_due_at:
        return False
    _price_check_due_at = loop_now + _PRICE_POLL_INTERVAL

    payload = await asyncio.to_thread(livefpl.fetch_price_payload)
    if not payload:
        return False
    current = livefpl.extract_prices(payload)
    if not current:
        return False

    previous = livefpl.load_price_snapshot()
    if previous is None:
        # First run on this database. Seed the baseline instead of reporting
        # a diff we cannot compute, so the next real change is reported.
        await asyncio.to_thread(livefpl.save_price_snapshot, current)
        logger.info("Seeded price baseline with %d players; no report to post", len(current))
        return False

    changes = livefpl.diff_prices(previous, payload)
    if not changes:
        return False

    text = await asyncio.to_thread(
        livefpl.build_price_changes_text,
        include_actual=True,
        include_potential=False,
        payload=payload,
        previous_prices=previous,
    )
    if not text:
        return False
    await client.send_message(target_channel, text, parse_mode="html")
    # Advance the baseline only after Telegram accepted the post, and to the
    # exact prices the report described. A failed send is retried next pass.
    await asyncio.to_thread(livefpl.save_price_snapshot, current)
    logger.info("Posted confirmed price changes (%d players moved)", len(changes))
    return True


def _already_posted(key: str) -> bool:
    val = db.query_scalar("SELECT value FROM last_updated WHERE key = ?", (key,))
    return val is not None


def _mark_posted(key: str) -> None:
    with db._connect() as conn:
        db._set_updated(conn, key)
