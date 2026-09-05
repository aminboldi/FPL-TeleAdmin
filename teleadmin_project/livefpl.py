"""Fetch FPL/LiveFPL data and format automated Telegram posts."""
import json
import logging
import time
from datetime import datetime, timezone

import requests

import database as db
import price_changes

logger = logging.getLogger(__name__)

_API_URL = "https://livefpl.us/api/games.json"
_FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
# Keep a useful watchlist of near-threshold players; the official predictor
# treats projected progress above 100% as expected to cross the boundary.
_PRICE_PREDICTION_THRESHOLD = 90.0
_PRICE_SNAPSHOT_KEY = "price_prediction_snapshot"

_POS_LETTER = {"Goalkeeper": "G", "Defender": "D", "Midfielder": "M", "Forward": "F"}
_ELEMENT_TYPE_POSITION = {1: "G", 2: "D", 3: "M", 4: "F"}

_HTML_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})

_EO_THRESHOLD = 10

_STAT_EMOJI = {
    "goals_scored": "\u26bd\ufe0f",
    "assists": "\U0001f170\ufe0f",
    "clean_sheets": "\U0001f6ab",
    "yellow_cards": "\U0001f538",
    "red_cards": "\u2666\ufe0f",
    "own_goals": "\U0001f17e",
    "defensive_contribution": "\u2705",
}


def _esc(text: str) -> str:
    return text.translate(_HTML_ESCAPES)


def _resolve_players(
    names: list[str], team_code: str | None = None,
    *, prefer_highest_ownership: bool = False,
) -> dict[str, dict | None]:
    """Resolve API names, optionally restricting candidates to one team.

    LiveFPL supplies players grouped by team for game posts.  A name-only
    lookup is unsafe because the FPL database can contain the same web name
    at multiple clubs (for example, Gomez).  Keep the unrestricted form for
    global lists, but make per-team callers constrain the SQL candidates.
    """
    if not names:
        return {}

    where = "1 = 1"
    params: list[str] = []
    if team_code:
        where += " AND upper(t.short_name) = upper(?)"
        params.append(team_code)

    results = db.query(
        f"""SELECT players.*, pos.singular_name AS pos_name,
                  t.short_name AS team_code, t.short_name_fa
           FROM players
           JOIN positions pos ON players.position_id = pos.id
           JOIN teams t ON players.team_id = t.id
           WHERE {where}""",
        tuple(params),
    )

    candidates: dict[str, list[dict]] = {name: [] for name in names}
    for player in results:
        for name in names:
            if db.player_name_matches(player, name):
                candidates[name].append(player)

    def ownership(player: dict) -> float:
        try:
            return float(player.get("selected_by_percent") or 0)
        except (TypeError, ValueError):
            return 0.0

    mapping: dict[str, dict] = {}
    for name, matches in candidates.items():
        if not matches:
            continue
        if prefer_highest_ownership:
            mapping[name] = max(matches, key=ownership)
        else:
            mapping[name] = max(
                matches,
                key=lambda player: float(player.get("total_points") or 0),
            )

    return mapping


# ── Per-game player points ──

def _build_stat_emojis(stats: list, element_id: int, events: list, db_player: dict | None) -> str:
    emojis = []

    for stat_name, value, _ in stats:
        if stat_name == "clean_sheets" and db_player:
            pos = db_player.get("pos_name", "")
            if pos in ("Midfielder", "Forward"):
                continue

        emoji = _STAT_EMOJI.get(stat_name)
        if emoji and value and value > 0:
            if stat_name == "defensive_contribution":
                emojis.append(emoji)
            else:
                for _ in range(int(value)):
                    emojis.append(emoji)

    for event in events:
        eid = event.get("identifier", "")
        if eid == "penalties_saved":
            for side in ("h", "a"):
                for entry in event.get(side, []):
                    if entry.get("element") == element_id:
                        for _ in range(entry.get("value", 0)):
                            emojis.append("\U0001f4db")
        elif eid == "penalties_missed":
            for side in ("h", "a"):
                for entry in event.get(side, []):
                    if entry.get("element") == element_id:
                        for _ in range(entry.get("value", 0)):
                            emojis.append("\u274c")

    return " ".join(emojis)


_CIRCLE_MAP: dict[tuple[int, int], str] = {
    (5, 999): "\U0001f7e2",
    (3, 4): "\u26aa",
    (0, 2): "\U0001f7e1",
    (-999, -1): "\U0001f534",
}


def _pts_circle(pts: int) -> str:
    for (lo, hi), circle in _CIRCLE_MAP.items():
        if lo <= pts <= hi:
            return circle
    return ""


def _game_player_line(
    player: dict | None, name: str, eo: float, pts: int,
    stats: list, element_id: int, events: list, is_bold: bool,
) -> str:
    if player:
        fa_name = player.get("web_name_fa") or player["web_name"]
        price = player["now_cost"] / 10
        pos_letter = _POS_LETTER.get(player.get("pos_name", ""), "?")
        name_part = _esc(fa_name)
        price_part = f"<b>{price:.1f}{pos_letter}</b>" if not is_bold else f"{price:.1f}{pos_letter}"
    else:
        name_part = _esc(name)
        price_part = ""

    eo_rounded = round(eo)
    eo_part = f"<b>{eo_rounded}%</b>" if not is_bold else f"{eo_rounded}%"
    pts_part = f"<b>{pts}</b>" if not is_bold else f"{pts}"

    stat_emojis = _build_stat_emojis(stats, element_id, events, player)
    emoji_str = f" {stat_emojis}" if stat_emojis else ""

    circle = _pts_circle(pts)

    display = f"{name_part} {price_part}" if price_part else name_part
    line = f"{display} \u0628\u0627 {eo_part} | \u0627\u0645\u062a\u06cc\u0627\u0632 {pts_part}{emoji_str}"

    if is_bold:
        line = f"<b>{line}</b>"

    return f"{circle} {line}"


def _build_team_section(
    players: list, events: list, team_code: str | None = None
) -> str:
    rows = [(p[1], p[0], p[3], p[4], p[5]) for p in players]
    rows.sort(key=lambda x: x[0], reverse=True)

    names = [r[1] for r in rows]
    db_players = _resolve_players(names, team_code=team_code)

    player_data = []
    for eo, p_name, p_pts, p_stats, p_element_id in rows:
        mins = 0
        for stat in p_stats:
            if stat[0] == "minutes":
                mins = stat[1]
                break
        player_data.append((eo, p_name, p_pts, p_stats, p_element_id, mins))

    by_mins = sorted(player_data, key=lambda x: x[5], reverse=True)
    starters = by_mins[:11]
    subs = by_mins[11:]

    starters.sort(key=lambda x: x[0], reverse=True)
    subs.sort(key=lambda x: x[0], reverse=True)

    high, low = [], []
    for eo, p_name, p_pts, p_stats, p_element_id, mins in starters:
        player = db_players.get(p_name)
        is_bold = round(eo) >= _EO_THRESHOLD
        line = _game_player_line(player, p_name, eo, p_pts, p_stats, p_element_id, events, is_bold)
        (high if is_bold else low).append(f"<blockquote>{line}</blockquote>")

    result = high[:]
    if high and low:
        result.append("")
    result.extend(low)

    if subs:
        sub_lines = []
        for eo, p_name, p_pts, p_stats, p_element_id, mins in subs:
            player = db_players.get(p_name)
            is_bold = round(eo) >= _EO_THRESHOLD
            line = _game_player_line(player, p_name, eo, p_pts, p_stats, p_element_id, events, is_bold)
            sub_lines.append(line)
        result.append(f"\n<blockquote>\n{'\n'.join(sub_lines)}\n</blockquote>")

    return "\n".join(result)


# Arabic tatweel is a strong RTL character, so this divider follows Persian
# text direction in Telegram instead of the neutral heavy-minus emoji.
_DIVIDER = "\u0640 \u0640 \u0640"


def build_game_text(fixture: dict) -> str | None:
    global _games_cache
    if _games_cache is None:
        try:
            _games_cache = _fetch_games()
        except Exception as e:
            logger.error("Failed to fetch LiveFPL API: %s", e)
            return None

    games = _games_cache
    home_en = fixture.get("home_en", "")
    away_en = fixture.get("away_en", "")

    game = None
    for g in games:
        if g[0] == home_en and g[1] == away_en:
            game = g
            break

    if not game:
        logger.warning("Game %s vs %s not found in API", home_en, away_en)
        return None

    home_fa = fixture.get("home_fa") or fixture.get("home_code", "")
    away_fa = fixture.get("away_fa") or fixture.get("away_code", "")

    parts = [
        f"\u0627\u0645\u062a\u06cc\u0627\u0632\u0627\u062a \u0641\u0627\u0646\u062a\u0632\u06cc \u0628\u0627\u0632\u06cc\u06a9\u0646\u0627\u0646 {home_fa} <b>{game[2]}</b> {away_fa} <b>{game[3]}</b> \u0628\u0627 \u0627\u062d\u062a\u0633\u0627\u0628 \u0628\u0648\u0646\u0633 \u067e\u06cc\u0634 \u0627\u0632 \u062a\u0627\u06cc\u06cc\u062f",
        "",
        _build_team_section(game[12], game[18], fixture.get("home_code")),
        "",
        _DIVIDER,
        "",
        _build_team_section(game[13], game[18], fixture.get("away_code")),
        "",
        "@EPL_Fantasy",
    ]
    return "\n".join(parts)


# ── Global EO leaderboard ──

def _eo_player_line(player: dict | None, name: str, eo: float) -> str:
    if player:
        fa_name = player.get("web_name_fa") or player["web_name"]
        price = player["now_cost"] / 10
        pos_letter = _POS_LETTER.get(player.get("pos_name", ""), "?")
        name_part = _esc(fa_name)
        price_part = f"{price:.1f}{pos_letter}"
    else:
        name_part = _esc(name)
        price_part = ""

    eo_rounded = round(eo)
    eo_part = f"{eo_rounded}%"
    is_bold = eo_rounded >= _EO_THRESHOLD

    display = f"{name_part} {price_part}" if price_part else name_part
    line = f"{display} \u0628\u0627 {eo_part}"

    if is_bold:
        line = f"<b>{line}</b>"

    # Every per-player line the channel posts is quoted, price reports and
    # this one alike, so a reader sees one list style everywhere.
    return f"<blockquote>{line}</blockquote>"


def build_eo_text(gameweek_id: int | None = None) -> str | None:
    global _games_cache
    if _games_cache is None:
        try:
            _games_cache = _fetch_games()
        except Exception as e:
            logger.error("Failed to fetch LiveFPL API: %s", e)
            return None

    games = _games_cache
    player_eo: dict[str, tuple[float, str]] = {}
    for g in games:
        for side in (g[12], g[13]):
            for p in side:
                name = p[0]
                eo = p[1]
                if name not in player_eo or eo > player_eo[name][0]:
                    team = g[0] if side is g[12] else g[1]
                    player_eo[name] = (eo, team)

    sorted_players = sorted(player_eo.items(), key=lambda x: x[1][0], reverse=True)
    names = [name for name, _ in sorted_players]
    # LiveFPL's global list has no team context.  Duplicate web names are
    # resolved to the FPL player with the larger selected-by percentage.
    db_players = _resolve_players(names, prefer_highest_ownership=True)

    if gameweek_id is None:
        gameweek_id = db.query_scalar(
            "SELECT id FROM gameweeks WHERE is_current = 1 OR is_next = 1 "
            "ORDER BY is_current DESC, id DESC LIMIT 1"
        )
    gameweek_label = f"GW{gameweek_id}" if gameweek_id else "EO"
    lines = [
        f"\u0645\u0627\u0644\u06a9\u06cc\u062a \u0645\u0648\u062b\u0631 (EO) \u0628\u0627\u0632\u06cc\u06a9\u0646\u0627\u0646 \u2014 {gameweek_label}",
        "",
    ]

    for idx, (name, (eo, team)) in enumerate(sorted_players):
        if round(eo) < _EO_THRESHOLD:
            break
        player = db_players.get(name)
        lines.append(_eo_player_line(player, name, eo))

        # Separators
        if idx == 10 and len([p for _, (e, _) in sorted_players if round(e) >= _EO_THRESHOLD]) > 11:
            lines.append("")
        if round(eo) >= 100:
            # Check if the NEXT player is below 100%
            if idx + 1 < len(sorted_players):
                _, (next_eo, _) = sorted_players[idx + 1]
                if round(next_eo) < 100:
                    lines.append("")

    if len(lines) == 2:
        lines.append("\u0647\u06cc\u0686 \u0628\u0627\u0632\u06cc\u06a9\u0646\u06cc \u0628\u0627\u0644\u0627\u06cc 10% \u0646\u06cc\u0633\u062a")

    lines.append("")
    lines.append("@EPL_Fantasy")
    return "\n".join(lines)


# ── API + DB ──

_games_cache = None
_games_backoff_until = 0.0
_games_backoff_seconds = 0.0


def _fetch_games():
    resp = requests.get(_API_URL, timeout=30)
    resp.raise_for_status()
    games = resp.json()
    if not isinstance(games, list):
        raise ValueError("LiveFPL games response has an unexpected shape")
    return games


def refresh_games() -> list | None:
    """Fetch the current LiveFPL snapshot, retaining the last good snapshot on failure."""
    global _games_cache, _games_backoff_until, _games_backoff_seconds
    now = time.monotonic()
    if now < _games_backoff_until:
        return _games_cache
    try:
        _games_cache = _fetch_games()
        _games_backoff_seconds = 0.0
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        if response is not None and response.status_code == 429:
            retry_after = None
            try:
                retry_after = float(response.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            _games_backoff_seconds = max(
                10.0,
                min(300.0, retry_after if retry_after is not None else (_games_backoff_seconds * 2 or 30.0)),
            )
            _games_backoff_until = now + _games_backoff_seconds
            logger.warning("LiveFPL returned HTTP 429; backing off for %.0fs", _games_backoff_seconds)
        else:
            logger.warning("Failed to refresh LiveFPL games API: %s", exc)
    except Exception as exc:
        logger.warning("Failed to refresh LiveFPL games API: %s", exc)
    return _games_cache


def is_game_finished(game: list) -> bool:
    """Handle the status values used by current and older LiveFPL responses."""
    return len(game) > 4 and str(game[4]).strip().lower() in {"done", "finished"}


def _fetch_fpl_bootstrap() -> dict:
    """Fetch the official FPL player payload used by the Price Changes page."""
    response = requests.get(_FPL_BOOTSTRAP_URL, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("Official FPL bootstrap response has an unexpected shape")
    return payload


def fetch_price_payload() -> dict | None:
    """Fetch the official bootstrap once so callers can reuse a single payload.

    Diffing prices, rendering the report, and saving the new baseline must all
    come from the *same* snapshot of the API.  Re-fetching between those steps
    silently loses any change that lands in between.
    """
    try:
        return _fetch_fpl_bootstrap()
    except Exception as exc:
        logger.error("Failed to fetch official FPL price data: %s", exc)
        return None


def extract_prices(payload: dict) -> dict[str, int]:
    """Map player id -> now_cost (in tenths) from a bootstrap payload."""
    return {
        str(player["id"]): int(player["now_cost"])
        for player in payload.get("elements") or []
        if isinstance(player, dict)
        and player.get("id") is not None
        and player.get("now_cost") is not None
    }


def load_price_snapshot() -> dict[str, int] | None:
    """Return the last recorded prices, or None when no baseline exists yet.

    None and {} mean different things: None means "never seeded, do not report
    a diff", while {} would mean "seeded but empty".
    """
    try:
        raw = db.query_scalar(
            "SELECT value FROM last_updated WHERE key = ?", (_PRICE_SNAPSHOT_KEY,)
        )
    except Exception:
        # The table is created during normal bot startup. Keep a dashboard
        # preview usable while a brand-new database is being initialized.
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    prices = value.get("prices") if isinstance(value, dict) else None
    if not isinstance(prices, dict):
        return None
    result: dict[str, int] = {}
    for key, cost in prices.items():
        try:
            result[str(key)] = int(cost)
        except (TypeError, ValueError):
            continue
    return result


def save_price_snapshot(prices: dict[str, int]) -> bool:
    """Persist the prices a report was built from, as the next baseline."""
    try:
        snapshot = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "prices": {str(k): int(v) for k, v in prices.items()},
        }
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO last_updated (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_PRICE_SNAPSHOT_KEY, json.dumps(snapshot, separators=(",", ":"))),
            )
        return True
    except Exception as exc:
        logger.warning("Could not save official price snapshot: %s", exc)
        return False


def diff_prices(
    previous: dict[str, int], payload: dict
) -> list[tuple[int, dict]]:
    """Return (delta_in_tenths, player) for every player whose price moved."""
    changes = []
    for player in payload.get("elements") or []:
        if not isinstance(player, dict) or player.get("now_cost") is None:
            continue
        old = previous.get(str(player.get("id")))
        if old is None:
            continue
        delta = int(player["now_cost"]) - int(old)
        if delta:
            changes.append((delta, player))
    return changes


def _load_db_players(player_ids: list[int]) -> dict[int, dict]:
    if not player_ids:
        return {}
    placeholders = ",".join("?" for _ in player_ids)
    rows = db.query(
        f"""SELECT players.id, players.web_name, players.web_name_fa,
                   players.now_cost, players.flag,
                   pos.singular_name AS pos_name,
                   t.short_name AS team_code, t.short_name_fa
            FROM players
            JOIN positions pos ON players.position_id = pos.id
            JOIN teams t ON players.team_id = t.id
            WHERE players.id IN ({placeholders})""",
        tuple(player_ids),
    )
    return {int(row["id"]): row for row in rows}


def _price_player_name(player: dict, db_player: dict | None) -> str:
    return (
        (db_player.get("web_name_fa") if db_player else None)
        or player.get("web_name")
        or "?"
    )


def _price_player_team(player: dict, db_player: dict | None, teams: dict[int, dict]) -> str:
    if db_player and db_player.get("short_name_fa"):
        return db_player["short_name_fa"]
    team = teams.get(int(player.get("team") or 0), {})
    return team.get("short_name") or player.get("team_code") or "?"


def _price_player_position(player: dict, db_player: dict | None) -> str:
    if db_player:
        return _POS_LETTER.get(db_player.get("pos_name", ""), "?")
    return _ELEMENT_TYPE_POSITION.get(int(player.get("element_type") or 0), "?")


def _price_move(
    player: dict,
    db_player: dict | None,
    teams: dict[int, dict],
    *,
    note: str = "",
    brief: bool = False,
) -> price_changes.PriceMove:
    """Resolve one official player record into a line of the channel's report.

    ``brief`` drops the flag and the club, which a confirmed change is worth
    spelling out but a watchlist of maybes is not.
    """
    return price_changes.PriceMove(
        name=_price_player_name(player, db_player),
        flag="" if brief else ((db_player.get("flag") or "") if db_player else ""),
        price=f"{int(player.get('now_cost') or 0) / 10:.1f}",
        position=_price_player_position(player, db_player),
        team="" if brief else _price_player_team(player, db_player, teams),
        note=note,
    )


def _price_ownership(player: dict) -> float:
    try:
        return float(str(player.get("selected_by_percent", 0)).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def build_price_changes_text(
    *,
    include_actual: bool = True,
    include_potential: bool = True,
    payload: dict | None = None,
    previous_prices: dict[str, int] | None = None,
) -> str | None:
    """Build a price report from the official FPL price-change data.

    ``payload`` lets the caller supply an already-fetched bootstrap so the
    diff, the report, and the saved baseline all describe the same instant.

    The confirmed section is a true diff against ``previous_prices`` (the last
    saved baseline).  Without a baseline it is omitted rather than guessed:
    ``cost_change_event`` counts the whole gameweek, not the last day, so
    using it as a stand-in produces a wrong "today's changes" list.

    All risers are retained; fallers are limited to players above 1% ownership
    and every list is ordered by ownership descending.
    """
    if payload is None:
        payload = fetch_price_payload()
        if payload is None:
            return None
    if previous_prices is None and include_actual:
        previous_prices = load_price_snapshot()

    players = [player for player in payload["elements"] if isinstance(player, dict)]
    team_rows = payload.get("teams") or []
    teams = {
        int(team["id"]): team
        for team in team_rows
        if isinstance(team, dict) and team.get("id") is not None
    }
    db_players = _load_db_players(
        [int(player["id"]) for player in players if player.get("id") is not None]
    )

    actual_risers = []
    actual_fallers = []
    have_baseline = bool(previous_prices)
    if include_actual and have_baseline:
        for delta_units, player in diff_prices(previous_prices, payload):
            if delta_units > 0:
                actual_risers.append((delta_units, player))
            elif delta_units < 0 and _price_ownership(player) > 1:
                actual_fallers.append((delta_units, player))

    potential_risers = []
    potential_fallers = []
    for player in players:
        projections = player.get("price_change_projections") or []
        projection = next(
            (
                item
                for item in projections
                if isinstance(item, dict) and item.get("offset") == 0
            ),
            projections[0] if projections and isinstance(projections[0], dict) else None,
        )
        try:
            progress = float(
                projection.get("projected_percent")
                if projection
                else player.get("price_change_percent")
            )
        except (TypeError, ValueError):
            continue
        if progress >= _PRICE_PREDICTION_THRESHOLD:
            potential_risers.append((progress, player))
        elif progress <= -_PRICE_PREDICTION_THRESHOLD and _price_ownership(player) > 1:
            potential_fallers.append((progress, player))

    actual_risers.sort(key=lambda item: _price_ownership(item[1]), reverse=True)
    actual_fallers.sort(key=lambda item: _price_ownership(item[1]), reverse=True)
    potential_risers.sort(key=lambda item: _price_ownership(item[1]), reverse=True)
    potential_fallers.sort(key=lambda item: _price_ownership(item[1]), reverse=True)

    def moves(entries, note=None, brief=False):
        return [
            _price_move(
                player,
                db_players.get(int(player["id"])),
                teams,
                note=note(value) if note else "",
                brief=brief,
            )
            for value, player in entries
        ]

    # Both reports render through the channel's own price layout, the one
    # written for relaying the source channel's posts. A confirmed change is
    # exactly what that layout describes, so it carries no extra labelling.
    # The watchlist is a list of maybes rather than a record, so it is kept
    # short: the player, the price, and how close the change is.
    reports = []
    if include_actual:
        reports.append(
            price_changes.format_price_report(
                moves(actual_risers),
                moves(actual_fallers),
                empty_note=(
                    "هنوز مبنایی برای مقایسه ثبت نشده است."
                    if not have_baseline
                    else "موردی از آخرین گزارش ثبت نشده است."
                ),
                footer=not include_potential,
            )
        )
    if include_potential:
        percent = lambda value: f"{round(value):.0f}%"  # noqa: E731
        reports.append(
            price_changes.format_price_report(
                moves(potential_risers, note=percent, brief=True),
                moves(potential_fallers, note=percent, brief=True),
                header=price_changes.prediction_header(),
                empty_note="موردی به آستانه پیش‌بینی نرسیده است.",
            )
        )
    return "\n\n".join(report for report in reports if report)


def get_finished_fixtures(gameweek_id: int | None = None) -> list[dict]:
    if gameweek_id is None:
        gameweek_id = db.query_scalar(
            "SELECT id FROM gameweeks WHERE is_current = 1 OR is_next = 1 ORDER BY id LIMIT 1"
        )
        if not gameweek_id:
            gameweek_id = db.query_scalar(
                "SELECT MAX(id) FROM gameweeks WHERE finished = 1"
            )
        if not gameweek_id:
            return []

    return db.query(
        """SELECT f.*, ht.short_name_fa as home_fa, at.short_name_fa as away_fa,
                  ht.name as home_en, at.name as away_en,
                  ht.short_name as home_code, at.short_name as away_code
           FROM fixtures f
           JOIN teams ht ON f.team_h = ht.id
           JOIN teams at ON f.team_a = at.id
           WHERE f.gameweek_id = ?
             AND (
                 f.finished = 1
                 OR (
                     f.finished = 0
                     AND f.team_h_score IS NOT NULL
                     AND f.team_a_score IS NOT NULL
                     AND f.minutes >= 90
                 )
             )
           ORDER BY f.kickoff_time""",
        (gameweek_id,),
    )


def get_fixtures() -> list[dict]:
    """Return all known fixtures for matching against LiveFPL's current snapshot."""
    return db.query(
        """SELECT f.*, ht.short_name_fa as home_fa, at.short_name_fa as away_fa,
                  ht.name as home_en, at.name as away_en,
                  ht.short_name as home_code, at.short_name as away_code
           FROM fixtures f
           JOIN teams ht ON f.team_h = ht.id
           JOIN teams at ON f.team_a = at.id
           ORDER BY f.kickoff_time"""
    )
