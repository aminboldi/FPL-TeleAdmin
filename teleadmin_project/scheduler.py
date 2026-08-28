"""Schedulers for automated posts and rapid live goal alerts."""
import asyncio
import json
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
_ACTUAL_PRICE_POSTED_KEY = "price_change_actual_posted"
_EO_POSTED_KEY = "eo_posted"
_PRICE_CHANGES_RESUME_DATE = date(2026, 8, 21)
_ACTUAL_PRICE_REPORT_DELAY = timedelta(minutes=2)
_DB_REFRESH_INTERVAL = 6 * 60 * 60
_DB_REFRESH_RETRY_INTERVAL = 30 * 60
_SCHEDULER_INTERVAL = 30
_GOAL_WATCH_INTERVAL = 10
_GOAL_IDLE_INTERVAL = 30
_LINEUP_LEAD_TIME = timedelta(minutes=75)
_GOAL_STATE_KEY = "live_goal_watcher_state"
_GOAL_LOCK = asyncio.Lock()


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

            # LiveFPL changes as matches progress. A one-time startup fetch
            # leaves all games permanently at their initial status and makes
            # both Done-game posts and the post-deadline EO snapshot invisible.
            # Goal alerts are handled separately from the official FPL fixture
            # feed by run_goal_watcher below.
            games = await asyncio.to_thread(livefpl.refresh_games)
            # Price reports use the official FPL bootstrap and must not be
            # blocked by a temporary outage or empty response from LiveFPL.
            if price_predictions_enabled:
                await _check_price_post(client, target_channel, games or [], now_iran)
            if not games:
                await asyncio.sleep(_SCHEDULER_INTERVAL)
                continue

            await _check_game_points(client, target_channel, games)

            await _check_eo_post(client, target_channel)

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


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_goal_state() -> dict:
    raw = db.query_scalar(
        "SELECT value FROM last_updated WHERE key = ?", (_GOAL_STATE_KEY,)
    )
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_goal_state(state: dict) -> None:
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO last_updated (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_GOAL_STATE_KEY, json.dumps(state, ensure_ascii=False, sort_keys=True)),
        )


def _fixture_is_live(fixture: dict, now_utc: datetime) -> bool:
    try:
        kickoff = datetime.strptime(
            str(fixture["kickoff_time"])[:19], "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    # Keep polling a little before kickoff and through the normal 120-minute
    # match window. This avoids hammering the feed for future/old fixtures.
    return kickoff - timedelta(minutes=5) <= now_utc <= kickoff + timedelta(minutes=155)


def _official_goal_candidates(game: dict, fixture: dict) -> dict[str, list[dict]]:
    """Return scorer candidates from official FPL fixture stats.

    Official fixture stats identify players by their stable FPL element ID.
    This avoids name collisions and keeps the scorer restricted to the two
    clubs in the fixture.  Own goals are assigned to the opposition score,
    while retaining the player's actual club for strict name resolution.
    """
    result = {"home": [], "away": []}
    team_codes = {
        "home": fixture.get("home_code", ""),
        "away": fixture.get("away_code", ""),
    }
    player_cache: dict[int, dict | None] = {}

    def player_for(element_id: int) -> dict | None:
        if element_id not in player_cache:
            player_cache[element_id] = db.query_one(
                "SELECT web_name FROM players WHERE id = ?", (element_id,)
            )
        return player_cache[element_id]

    stats = game.get("stats") if isinstance(game, dict) else None
    if not isinstance(stats, list):
        return result
    for stat in stats:
        if not isinstance(stat, dict):
            continue
        identifier = str(stat.get("identifier", "")).casefold()
        if identifier not in {"goals_scored", "own_goals"}:
            continue
        for side in ("h", "a"):
            player_side = "home" if side == "h" else "away"
            scoring_side = player_side
            entries = stat.get(side)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                element_id = _as_int(entry.get("element"), 0)
                player = player_for(element_id) if element_id else None
                if not player:
                    continue
                count = max(0, _as_int(entry.get("value"), 0))
                if not count:
                    continue
                if identifier == "own_goals":
                    scoring_side = "away" if player_side == "home" else "home"
                    kind = "own_goal"
                else:
                    kind = "goal"
                for _ in range(count):
                    result[scoring_side].append(
                        {
                            "id": element_id,
                            "name": player.get("web_name", ""),
                            "team_code": team_codes["home" if side == "h" else "away"],
                            "kind": kind,
                        }
                    )
    return result


def _goal_key(
    fixture_id: int,
    home_score: int,
    away_score: int,
    scoring_side: str | None = None,
    side_goal_no: int | None = None,
) -> str:
    key = f"live_goal_{int(fixture_id)}_{int(home_score)}_{int(away_score)}"
    # The side suffix permits two goals detected in one poll (for example a
    # simultaneous 1-1 update) to retain separate Telegram messages.
    if scoring_side and side_goal_no:
        key += f"_{scoring_side}{int(side_goal_no)}"
    return key


def _candidate_for_row(row: dict, candidates: dict[str, list[dict]]) -> dict | None:
    side = row.get("scoring_side")
    ordinal = _as_int(row.get("side_goal_no"), 0)
    if side not in candidates or ordinal < 1:
        return None
    values = candidates[side]
    return values[ordinal - 1] if len(values) >= ordinal else None


def _provisional_goal_text(fixture: dict, row: dict, candidate: dict | None) -> str:
    return alerts.format_provisional_goal(
        home_team=fixture.get("home_en", ""),
        away_team=fixture.get("away_en", ""),
        home_code=fixture.get("home_code", ""),
        away_code=fixture.get("away_code", ""),
        home_score=_as_int(row.get("home_score")),
        away_score=_as_int(row.get("away_score")),
        scorer_name=(candidate or {}).get("name", ""),
        scorer_team_code=(candidate or {}).get("team_code", ""),
        own_goal=(candidate or {}).get("kind") == "own_goal",
    )


def _strike_goal_text(text: str) -> str:
    """Strike a reverted goal's content while leaving the channel signature."""
    marker = "\n@EPL_Fantasy"
    if marker in text:
        body, signature = text.rsplit(marker, 1)
        if "<s>" in body or "<strike>" in body or "<del>" in body:
            return text
        return f"<s>{body}</s>{marker}{signature}"
    if "<s>" in text or "<strike>" in text or "<del>" in text:
        return text
    return f"<s>{text}</s>"


async def _cancel_reverted_goal_alerts(
    client,
    target_channel: str,
    fixture: dict,
    home_score: int,
    away_score: int,
) -> None:
    """Strike goal posts whose scoreline disappeared after a VAR review."""
    fixture_id = _as_int(fixture.get("id"), 0)
    if not fixture_id:
        return
    for row in db.list_goal_alerts(fixture_id):
        if row.get("cancelled"):
            continue
        if _as_int(row.get("home_score")) <= home_score and _as_int(row.get("away_score")) <= away_score:
            continue
        struck = _strike_goal_text(row.get("text", ""))
        if struck == row.get("text"):
            db.update_goal_alert(row["goal_key"], struck, cancelled=True)
            continue
        try:
            await client.edit_message(
                target_channel,
                row["target_msg_id"],
                struck,
                parse_mode="html",
            )
        except Exception:
            logger.exception("Could not strike reverted goal %s", row.get("goal_key"))
            continue
        db.update_goal_alert(row["goal_key"], struck, cancelled=True)
        logger.info("Struck reverted goal alert %s after score correction", row.get("goal_key"))


async def _check_goal_alerts(client, target_channel: str, games: list[dict]) -> None:
    """Create/edit fast goal posts from official FPL fixture snapshots."""
    fixtures = livefpl.get_fixtures()
    fixture_map = {_as_int(fixture.get("id")): fixture for fixture in fixtures}
    state = _load_goal_state()
    now_utc = datetime.now(tz=timezone.utc)

    async with _GOAL_LOCK:
        for game in games:
            if not isinstance(game, dict):
                continue
            fixture = fixture_map.get(_as_int(game.get("id")))
            if not fixture or not _fixture_is_live(fixture, now_utc):
                continue
            home_score = _as_int(game.get("team_h_score"))
            away_score = _as_int(game.get("team_a_score"))
            fixture_id = _as_int(fixture.get("id"), 0)
            if not fixture_id:
                continue
            candidates = _official_goal_candidates(game, fixture)
            state_key = str(fixture_id)
            previous = state.get(state_key) or {}
            previous_home = _as_int(previous.get("home_score"), _as_int(fixture.get("team_h_score")))
            previous_away = _as_int(previous.get("away_score"), _as_int(fixture.get("team_a_score")))
            if home_score < previous_home or away_score < previous_away:
                await _cancel_reverted_goal_alerts(
                    client, target_channel, fixture, home_score, away_score
                )
            old_home = previous_home
            old_away = previous_away
            # A stale DB/API snapshot must never make us invent a negative
            # goal. Score decreases are handled by adopting the new baseline.
            old_home = min(old_home, home_score)
            old_away = min(old_away, away_score)

            for side, old_score, new_score in (
                ("home", old_home, home_score),
                ("away", old_away, away_score),
            ):
                delta = max(0, new_score - old_score)
                for step in range(1, delta + 1):
                    score_home = old_home + step if side == "home" else home_score
                    score_away = old_away + step if side == "away" else away_score
                    side_goal_no = old_score + step
                    existing_scoreline = db.find_goal_alert(
                        fixture.get("home_code", ""), fixture.get("away_code", ""),
                        score_home, score_away,
                    )
                    if existing_scoreline and existing_scoreline.get("cancelled"):
                        if existing_scoreline.get("confirmed"):
                            continue
                        restored_candidate = candidates[side][side_goal_no - 1] if len(candidates[side]) >= side_goal_no else None
                        restored_text = _provisional_goal_text(
                            fixture, existing_scoreline, restored_candidate
                        )
                        try:
                            await client.edit_message(
                                target_channel,
                                existing_scoreline["target_msg_id"],
                                restored_text,
                                parse_mode="html",
                            )
                        except Exception:
                            logger.exception("Could not restore re-awarded goal %s", existing_scoreline.get("goal_key"))
                        else:
                            db.update_goal_alert(
                                existing_scoreline["goal_key"],
                                restored_text,
                                cancelled=False,
                                scorer_id=(restored_candidate or {}).get("id") or None,
                                scorer_kind=(restored_candidate or {}).get("kind"),
                            )
                        continue
                    if existing_scoreline and (
                        existing_scoreline.get("confirmed")
                        or existing_scoreline.get("scoring_side") == side
                    ):
                        continue
                    row = {
                        "home_score": score_home,
                        "away_score": score_away,
                        "scoring_side": side,
                        "side_goal_no": side_goal_no,
                    }
                    candidate = candidates[side][side_goal_no - 1] if len(candidates[side]) >= side_goal_no else None
                    text = _provisional_goal_text(fixture, row, candidate)
                    try:
                        message = await client.send_message(target_channel, text, parse_mode="html")
                    except Exception:
                        logger.exception("Could not post provisional goal for %s vs %s", fixture.get("home_en"), fixture.get("away_en"))
                        continue
                    message_id = getattr(message, "id", None)
                    if message_id is None:
                        continue
                    key = _goal_key(
                        fixture_id, score_home, score_away, side, side_goal_no
                    )
                    db.store_goal_alert(
                        key, fixture_id, target_channel, message_id,
                        fixture.get("home_code", ""), fixture.get("away_code", ""),
                        score_home, score_away, side, side_goal_no, text,
                        scorer_id=(candidate or {}).get("id") or None,
                        scorer_kind=(candidate or {}).get("kind"),
                    )
                    logger.info("Posted provisional goal for %s vs %s", fixture.get("home_en"), fixture.get("away_en"))

            for row in db.list_pending_goal_alerts(fixture_id):
                candidate = _candidate_for_row(row, candidates)
                if not candidate:
                    continue
                if (
                    _as_int(row.get("scorer_id"), 0) == _as_int(candidate.get("id"), 0)
                    and row.get("scorer_kind") == candidate.get("kind")
                ):
                    continue
                text = _provisional_goal_text(fixture, row, candidate)
                try:
                    await client.edit_message(target_channel, row["target_msg_id"], text, parse_mode="html")
                except Exception:
                    logger.exception("Could not enrich provisional goal %s", row.get("goal_key"))
                    continue
                db.update_goal_alert(
                    row["goal_key"], text,
                    scorer_id=_as_int(candidate.get("id"), 0) or None,
                    scorer_kind=candidate.get("kind"),
                )

            state[state_key] = {"home_score": home_score, "away_score": away_score}
        _save_goal_state(state)


async def run_goal_watcher(client, target_channel: str) -> None:
    """Poll the official FPL fixture feed rapidly for live goal changes."""
    logger.info("Live goal watcher started (%ss interval)", _GOAL_WATCH_INTERVAL)
    await asyncio.sleep(5)
    sleep_for = _GOAL_IDLE_INTERVAL
    while True:
        try:
            current_target = runtime_config.get("TARGET_CHANNEL_ID") or target_channel
            if current_target:
                games = await asyncio.to_thread(livefpl.refresh_official_fixtures)
                if games:
                    await _check_goal_alerts(client, current_target, games)
                    fixtures = livefpl.get_fixtures()
                    now_utc = datetime.now(tz=timezone.utc)
                    sleep_for = (
                        _GOAL_WATCH_INTERVAL
                        if any(
                            (fixture := next(
                                (
                                    value
                                    for value in fixtures
                                    if _as_int(value.get("id")) == _as_int(game.get("id"))
                                ),
                                None,
                            ))
                            and _fixture_is_live(fixture, now_utc)
                            for game in games
                        )
                        else _GOAL_IDLE_INTERVAL
                    )
                else:
                    sleep_for = _GOAL_IDLE_INTERVAL
        except Exception:
            logger.exception("Live goal watcher error")
            sleep_for = _GOAL_IDLE_INTERVAL
        await asyncio.sleep(sleep_for)


def _fixture_for_parsed_alert(parsed) -> dict | None:
    if parsed.home_team_code and parsed.away_team_code:
        return db.query_one(
            """
            SELECT f.id, f.team_h_score, f.team_a_score,
                   ht.name AS home_en, at.name AS away_en,
                   ht.short_name AS home_code, at.short_name AS away_code
            FROM fixtures f
            JOIN teams ht ON ht.id = f.team_h
            JOIN teams at ON at.id = f.team_a
            WHERE upper(ht.short_name) = upper(?) AND upper(at.short_name) = upper(?)
            ORDER BY f.kickoff_time DESC LIMIT 1
            """,
            (parsed.home_team_code, parsed.away_team_code),
        )
    return db.query_one(
        """
        SELECT f.id, f.team_h_score, f.team_a_score,
               ht.name AS home_en, at.name AS away_en,
               ht.short_name AS home_code, at.short_name AS away_code
        FROM fixtures f
        JOIN teams ht ON ht.id = f.team_h
        JOIN teams at ON at.id = f.team_a
        WHERE lower(ht.name) = lower(?) AND lower(at.name) = lower(?)
        ORDER BY f.kickoff_time DESC LIMIT 1
        """,
        (parsed.home_team, parsed.away_team),
    )


def _contains_goal(parsed) -> bool:
    return any(
        action.type in {"Goal", "goal_penalty", "own_goal"}
        for action in getattr(parsed, "actions", [])
    )


async def reconcile_confirmed_goal(client, target_channel: str, parsed, text: str) -> int | None:
    """Edit a provisional API post when the source alert confirms the goal."""
    if not _contains_goal(parsed):
        return None
    fixture = _fixture_for_parsed_alert(parsed)
    if not fixture:
        return None
    async with _GOAL_LOCK:
        row = db.find_goal_alert(
            fixture["home_code"], fixture["away_code"],
            parsed.home_score, parsed.away_score,
        )
        if not row:
            return None
        # A duplicate source post should still go through the normal alert
        # deduplication path. Re-edit only when a later source message really
        # changes the confirmed representation (for example a goal becoming
        # an own goal).
        if row.get("confirmed") and row.get("text") == text:
            return None
        try:
            await client.edit_message(target_channel, row["target_msg_id"], text, parse_mode="html")
        except Exception:
            logger.exception("Could not replace provisional goal %s", row["goal_key"])
            return None
        db.update_goal_alert(row["goal_key"], text, confirmed=True, cancelled=False)
        return int(row["target_msg_id"])


def register_confirmed_goal(target_channel: str, parsed, text: str, message_id: int) -> None:
    """Record a source-first goal so the API watcher will not duplicate it."""
    if not _contains_goal(parsed):
        return
    fixture = _fixture_for_parsed_alert(parsed)
    if not fixture:
        return
    key = _goal_key(fixture["id"], parsed.home_score, parsed.away_score)
    existing = db.get_goal_alert(key)
    if existing:
        return
    db.store_goal_alert(
        key, fixture["id"], target_channel, message_id,
        fixture["home_code"], fixture["away_code"],
        parsed.home_score, parsed.away_score, None, None, text,
        confirmed=True,
    )


async def _check_price_post(client, target_channel, games, now_iran):
    if now_iran.date() < _PRICE_CHANGES_RESUME_DATE:
        return

    now_utc = now_iran - _IRAN_OFFSET

    # Confirmed changes happen at 00:00 GMT regardless of fixture status.
    # Wait briefly for the official API to publish the new prices, then post
    # once for this UTC date.
    actual_key = f"{_ACTUAL_PRICE_POSTED_KEY}_{now_utc.date().isoformat()}"
    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if now_utc >= midnight_utc + _ACTUAL_PRICE_REPORT_DELAY and not _already_posted(actual_key):
        await _publish_price_report(
            client,
            target_channel,
            actual_key,
            include_actual=True,
            include_potential=False,
        )

    # The potential-change watchlist is published before the next midnight
    # update, at 23:30 Iran time. It is independent of live matches.
    if now_iran.hour < 23 or (now_iran.hour == 23 and now_iran.minute < 30):
        return
    prediction_key = f"{_PRICE_POSTED_KEY}_{now_iran.date().isoformat()}"
    if _already_posted(prediction_key):
        return
    await _publish_price_report(
        client,
        target_channel,
        prediction_key,
        include_actual=False,
        include_potential=True,
    )


async def _publish_price_report(
    client,
    target_channel: str,
    key: str,
    *,
    include_actual: bool,
    include_potential: bool,
) -> bool:
    text = await asyncio.to_thread(
        livefpl.build_price_changes_text,
        include_actual=include_actual,
        include_potential=include_potential,
    )
    if not text:
        return False
    await client.send_message(target_channel, text, parse_mode="html")
    # Save the baseline only after Telegram accepted the report. The next
    # actual report can then identify changes since this post.
    await asyncio.to_thread(livefpl.save_price_snapshot)
    _mark_posted(key)
    logger.info(
        "Posted official price change report (%s)",
        "actual" if include_actual else "prediction",
    )
    return True


def _already_posted(key: str) -> bool:
    val = db.query_scalar("SELECT value FROM last_updated WHERE key = ?", (key,))
    return val is not None


def _mark_posted(key: str) -> None:
    with db._connect() as conn:
        db._set_updated(conn, key)
