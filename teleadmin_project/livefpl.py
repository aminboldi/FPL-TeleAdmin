"""Fetch LiveFPL API and format player points/EO as text for Telegram posts."""
import logging

import requests

import database as db

logger = logging.getLogger(__name__)

_API_URL = "https://livefpl.us/api/games.json"
_PRICES_URL = "https://livefpl.us/api/prices.json"
_PRICE_RISE_THRESHOLD = 0.9
_PRICE_FALL_THRESHOLD = -0.9

_POS_LETTER = {"Goalkeeper": "G", "Defender": "D", "Midfielder": "M", "Forward": "F"}

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


def _resolve_players(names: list[str]) -> dict[str, dict | None]:
    import unicodedata

    def normalize(text):
        return "".join(
            c
            for c in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(c)
        )

    if not names:
        return {}

    placeholders = ",".join("?" for _ in names)
    lower_names = [normalize(n).lower() for n in names]

    results = db.query(
        f"""SELECT players.*, pos.singular_name AS pos_name,
                  t.short_name AS team_code, t.short_name_fa
           FROM players
           JOIN positions pos ON players.position_id = pos.id
           JOIN teams t ON players.team_id = t.id
           WHERE lower(search_name) IN ({placeholders})
              OR lower(alias) IN ({placeholders})
              OR lower(web_name) IN ({placeholders})""",
        tuple(lower_names + lower_names + lower_names),
    )

    mapping: dict[str, dict] = {}
    for player in results:
        for name in names:
            norm = normalize(name).lower()
            if (
                normalize(player["web_name"]).lower() == norm
                or normalize(player.get("alias") or "").lower() == norm
                or normalize(player["search_name"]).lower() == norm
            ):
                mapping[name] = player
                break

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


def _build_team_section(players: list, events: list) -> str:
    rows = [(p[1], p[0], p[3], p[4], p[5]) for p in players]
    rows.sort(key=lambda x: x[0], reverse=True)

    names = [r[1] for r in rows]
    db_players = _resolve_players(names)

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


_DIVIDER = "\u2796 \u2796 \u2796"


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
        _build_team_section(game[12], game[18]),
        "",
        _DIVIDER,
        "",
        _build_team_section(game[13], game[18]),
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

    return line


def build_eo_text() -> str | None:
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
    db_players = _resolve_players(names)

    lines = ["\u0645\u0627\u0644\u06a9\u06cc\u062a \u0645\u0648\u062b\u0631 (EO) \u0628\u0627\u0632\u06cc\u06a9\u0646\u0627\u0646 \u2014 GW38", ""]

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


def _fetch_games():
    resp = requests.get(_API_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def build_price_changes_text() -> str | None:
    """Fetch prices API and list players predicted to rise/fall tonight."""
    try:
        resp = requests.get(_PRICES_URL, timeout=30)
        resp.raise_for_status()
        prices = resp.json()
    except Exception as e:
        logger.error("Failed to fetch prices API: %s", e)
        return None

    risers = []
    fallers = []
    for p in prices.values():
        if not isinstance(p, dict):
            continue
        progress = p.get("progress", 0)
        if progress >= _PRICE_RISE_THRESHOLD:
            risers.append((p["name"], p.get("team", ""), progress, p.get("cost", 0), p.get("type", "")))
        elif progress <= _PRICE_FALL_THRESHOLD:
            fallers.append((p["name"], p.get("team", ""), progress, p.get("cost", 0), p.get("type", "")))

    risers.sort(key=lambda x: x[2], reverse=True)
    fallers.sort(key=lambda x: x[2])

    names = [r[0] for r in risers] + [f[0] for f in fallers]
    db_players = _resolve_players(names)

    lines = ["\u067e\u06cc\u0634\u200c\u0628\u06cc\u0646\u06cc \u062a\u063a\u06cc\u06cc\u0631\u0627\u062a \u0642\u06cc\u0645\u062a \u0627\u0645\u0634\u0628", ""]

    if risers:
        lines.append("\U0001f53c \u0627\u0641\u0632\u0627\u06cc\u0634:")
        lines.append("")
        for name, team, prog, cost, ptype in risers:
            player = db_players.get(name)
            line = _price_player_line(player, name, team, prog, cost, ptype)
            lines.append(line)
        lines.append("")

    if fallers:
        lines.append("\U0001f53d \u06a9\u0627\u0647\u0634:")
        lines.append("")
        for name, team, prog, cost, ptype in fallers:
            player = db_players.get(name)
            line = _price_player_line(player, name, team, prog, cost, ptype)
            lines.append(line)
        lines.append("")

    if not risers and not fallers:
        lines.append("\u0647\u06cc\u0686 \u062a\u063a\u06cc\u06cc\u0631 \u0642\u06cc\u0645\u062a\u06cc \u067e\u06cc\u0634\u200c\u0628\u06cc\u0646\u06cc \u0646\u0645\u06cc\u200c\u0634\u0648\u062f")

    lines.append("@EPL_Fantasy")
    return "\n".join(lines)


def _price_player_line(player: dict | None, name: str, team: str, progress: float, cost: float, ptype: str) -> str:
    if player:
        fa_name = player.get("web_name_fa") or player["web_name"]
        price = player["now_cost"] / 10
        pos_letter = _POS_LETTER.get(player.get("pos_name", ""), "?")
        name_part = _esc(fa_name)
        price_part = f"<b>{price:.1f}{pos_letter}</b>"
    else:
        name_part = _esc(name)
        price_part = f"<b>{cost}{ptype[0] if ptype else '?'}</b>"

    prog_pct = round(progress * 100)
    line = f"{name_part} {price_part} \u0628\u0627 <b>{prog_pct}%</b>"
    return f"<blockquote>{line}</blockquote>"


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
           WHERE f.gameweek_id = ? AND f.finished = 1
           ORDER BY f.kickoff_time""",
        (gameweek_id,),
    )
