"""Parse English game-action alerts and format them in Farsi."""
import re
import unicodedata
from dataclasses import dataclass, field

import database as db

_POS_LETTER = {"Goalkeeper": "G", "Defender": "D", "Midfielder": "M", "Forward": "F"}

_HTML_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def _esc(text: str) -> str:
    return text.translate(_HTML_ESCAPES)


def _normalize(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


_ACTION_RE = re.compile(
    r"^(Goal|Assist|Red card|Penalty missed|Penalty saved|Red Card|Penalty Missed|Penalty Saved)\s*[-–—]\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)

_SCORE_RE = re.compile(
    r"^(.+?)\s+(\d+)\s*[-–—]\s*(\d+)\s+(.+?)\s*\((\d+(?:\+\d+)?)\s*(?:mins?|min)?\s*\)\s*$",
    re.MULTILINE,
)

_SCORE_SIMPLE_RE = re.compile(
    r"^(.+?)\s+(\d+)\s*[-–—]\s*(\d+)\s+(.+?)\s*$",
    re.MULTILINE,
)

_HASHTAG_RE = re.compile(r"#FPL\s*#(\w{3})(\w{3})", re.IGNORECASE)


@dataclass
class Action:
    type: str
    player_name: str
    detail: str | None = None


@dataclass
class ParsedAlert:
    actions: list[Action] = field(default_factory=list)
    home_team: str = ""
    away_team: str = ""
    home_score: int = 0
    away_score: int = 0
    minute: str = ""
    home_team_code: str = ""
    away_team_code: str = ""


def is_game_alert(text: str) -> bool:
    if not text:
        return False
    has_action = bool(_ACTION_RE.search(text))
    has_score = bool(_SCORE_RE.search(text) or _SCORE_SIMPLE_RE.search(text))
    return has_action and has_score


def parse(text: str) -> ParsedAlert | None:
    alert = ParsedAlert()

    for m in _ACTION_RE.finditer(text):
        action_type = m.group(1).capitalize()
        raw = m.group(2).strip()
        detail = None

        if action_type in ("Goal", "Goal_penalty"):
            own_goal_m = re.match(r"^own goal\s*\((.+)\)$", raw, re.IGNORECASE)
            pen_m = re.match(r"^(.+?)\s*\(pen(?:alty)?\)$", raw, re.IGNORECASE)
            if own_goal_m:
                action_type = "own_goal"
                raw = own_goal_m.group(1)
            elif pen_m:
                action_type = "goal_penalty"
                raw = pen_m.group(1)
            else:
                action_type = "Goal"
        elif action_type in ("Red card", "Red Card"):
            action_type = "Red card"

        alert.actions.append(Action(type=action_type, player_name=raw, detail=detail))

    score_m = _SCORE_RE.search(text) or _SCORE_SIMPLE_RE.search(text)
    if score_m:
        alert.home_team = score_m.group(1).strip()
        alert.home_score = int(score_m.group(2))
        alert.away_score = int(score_m.group(3))
        alert.away_team = score_m.group(4).strip()
        try:
            alert.minute = score_m.group(5)
        except IndexError:
            alert.minute = "?"

    hash_m = _HASHTAG_RE.search(text)
    if hash_m:
        alert.home_team_code = hash_m.group(1).upper()
        alert.away_team_code = hash_m.group(2).upper()

    if not alert.actions:
        return None
    return alert


def _resolve_player(
    name: str, home_team_code: str, away_team_code: str
) -> dict | None:
    normalized = _normalize(name)
    results = db.query(
        """SELECT players.*, pos.singular_name AS pos_name, t.short_name AS team_code,
                  t.name_fa, t.short_name_fa
           FROM players
           JOIN positions pos ON players.position_id = pos.id
           JOIN teams t ON players.team_id = t.id
           WHERE lower(web_name) = lower(?)
              OR lower(alias) = lower(?)
              OR lower(search_name) LIKE lower(?)
           ORDER BY
             CASE WHEN t.short_name IN (?, ?) THEN 0 ELSE 1 END,
             total_points DESC""",
        (normalized, normalized, f"%{normalized}%", home_team_code, away_team_code),
    )
    return results[0] if results else None


def _price_display(player: dict) -> str:
    price = player["now_cost"] / 10
    pos_letter = _POS_LETTER.get(player["pos_name"], "?")
    return f"<b>{price:.1f}{pos_letter}</b>"


def _lookup_team(code: str, fallback_name: str) -> dict:
    if code:
        result = db.query_one(
            "SELECT name_fa, short_name_fa FROM teams WHERE short_name=?",
            (code,),
        )
        if result:
            return result
    result = db.query_one(
        "SELECT name_fa, short_name_fa FROM teams WHERE lower(name) LIKE ?",
        (f"%{fallback_name.lower()}%",),
    )
    return result or {"short_name_fa": fallback_name}


def format_farsi(alert: ParsedAlert) -> str | None:
    home = _lookup_team(alert.home_team_code, alert.home_team)
    away = _lookup_team(alert.away_team_code, alert.away_team)

    header = (
        f"'<b>{alert.minute}</b> | "
        f"{home['short_name_fa']} <b>{alert.home_score}</b> "
        f"{away['short_name_fa']} <b>{alert.away_score}</b>"
    )

    lines = [header, ""]

    for action in alert.actions:
        if action.type == "own_goal":
            player = _resolve_player(
                action.player_name,
                alert.home_team_code,
                alert.away_team_code,
            )
            if not player:
                lines.append(f"\u26bd {action.player_name} ({_esc('گل بخودی')})")
            else:
                lines.append(
                    f"\u26bd {player['web_name_fa'] or player['web_name']} "
                    f"{_price_display(player)} "
                    f"({_esc('گل بخودی')})"
                )

        elif action.type in ("Goal", "goal_penalty"):
            player = _resolve_player(
                action.player_name,
                alert.home_team_code,
                alert.away_team_code,
            )
            if not player:
                lines.append(f"\u26bd {action.player_name}")
            else:
                name = player["web_name_fa"] or player["web_name"]
                price = _price_display(player)
                if action.type == "goal_penalty":
                    lines.append(
                        f"\u26bd {name} {price} ({_esc('پنالتی')})"
                    )
                else:
                    lines.append(f"\u26bd {name} {price}")

        elif action.type == "Assist":
            player_name = action.player_name.lower()
            if player_name == "none":
                lines.append(f"\U0001f170\ufe0f {_esc('ندارد')}")
            elif player_name == "tbd":
                lines.append(f"\U0001f170\ufe0f {_esc('در دست بررسی')}")
            else:
                player = _resolve_player(
                    action.player_name,
                    alert.home_team_code,
                    alert.away_team_code,
                )
                if not player:
                    lines.append(f"\U0001f170\ufe0f {action.player_name}")
                else:
                    name = player["web_name_fa"] or player["web_name"]
                    lines.append(
                        f"\U0001f170\ufe0f {name} {_price_display(player)}"
                    )

        elif action.type == "Red card":
            player = _resolve_player(
                action.player_name,
                alert.home_team_code,
                alert.away_team_code,
            )
            if not player:
                lines.append(
                    f"\u2666 {_esc('اخراج')} {action.player_name}"
                )
            else:
                player_team = db.query_one(
                    "SELECT short_name_fa FROM teams WHERE short_name=?",
                    (player["team_code"],),
                )
                team_str = (
                    player_team["short_name_fa"]
                    if player_team
                    else player["team_code"]
                )
                lines.append(
                    f"\u2666 {_esc('اخراج')} "
                    f"{player['web_name_fa'] or player['web_name']} "
                    f"{_price_display(player)} ({team_str})"
                )

        elif action.type == "Penalty missed":
            player = _resolve_player(
                action.player_name,
                alert.home_team_code,
                alert.away_team_code,
            )
            if not player:
                lines.append(f"\u274c {action.player_name}")
            else:
                lines.append(
                    f"\u274c {player['web_name_fa'] or player['web_name']} "
                    f"{_price_display(player)}"
                )

        elif action.type == "Penalty saved":
            player = _resolve_player(
                action.player_name,
                alert.home_team_code,
                alert.away_team_code,
            )
            if not player:
                lines.append(f"\U0001f4db {action.player_name}")
            else:
                lines.append(
                    f"\U0001f4db {player['web_name_fa'] or player['web_name']} "
                    f"{_price_display(player)} ({_esc('مهار پنالتی')})"
                )

    lines.append("")
    lines.append("@EPL_Fantasy")
    return "\n".join(lines)


# ── Line-up parsing ──

_LINEUP_HEADER_RE = re.compile(r"^LINE-UPS\s*\|\s*#(\w{3})(\w{3})", re.IGNORECASE)
_LINEUP_TEAM_RE = re.compile(r"^[^\s]+ (\w{3}):\s*(.+)$", re.MULTILINE)

_IRAN_OFFSET_MINUTES = 210  # UTC + 3:30


def _utc_to_iran(utc_str: str) -> str:
    from datetime import datetime, timedelta, timezone as tz

    try:
        dt = datetime.strptime(utc_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=tz.utc)
        local = dt + timedelta(minutes=_IRAN_OFFSET_MINUTES)
        return local.strftime("%H:%M")
    except (ValueError, IndexError):
        return ""


def _find_fixture_kickoff(home_code: str, away_code: str) -> str:
    result = db.query_one(
        """SELECT kickoff_time FROM fixtures f
           JOIN teams ht ON f.team_h = ht.id
           JOIN teams at ON f.team_a = at.id
           WHERE ht.short_name = ? AND at.short_name = ?
              AND f.gameweek_id = (SELECT MAX(id) FROM gameweeks WHERE is_next = 1 OR is_current = 1)
           LIMIT 1""",
        (home_code, away_code),
    )
    if not result:
        result = db.query_one(
            """SELECT kickoff_time FROM fixtures f
               JOIN teams ht ON f.team_h = ht.id
               JOIN teams at ON f.team_a = at.id
               WHERE ht.short_name = ? AND at.short_name = ?
               ORDER BY f.gameweek_id DESC LIMIT 1""",
            (home_code, away_code),
        )
    return _utc_to_iran(result["kickoff_time"]) if result else ""


def is_lineup(text: str) -> bool:
    if not text:
        return False
    return bool(_LINEUP_HEADER_RE.search(text))


def parse_lineup(text: str) -> dict | None:
    header_m = _LINEUP_HEADER_RE.search(text)
    if not header_m:
        return None

    result = {
        "home_code": header_m.group(1).upper(),
        "away_code": header_m.group(2).upper(),
        "teams": [],
    }

    for m in _LINEUP_TEAM_RE.finditer(text):
        team_code = m.group(1).upper()
        names_str = m.group(2)
        names = [n.strip() for n in names_str.split(",") if n.strip()]
        result["teams"].append({"code": team_code, "players": names})

    return result if result["teams"] else None


def format_lineup(parsed: dict) -> str | None:
    home = _lookup_team(parsed["home_code"], parsed["home_code"])
    away = _lookup_team(parsed["away_code"], parsed["away_code"])
    kickoff = _find_fixture_kickoff(parsed["home_code"], parsed["away_code"])
    time_str = f" | {kickoff}" if kickoff else ""

    lines = [
        f"<b>📋 ترکیب | {home['short_name_fa']} - {away['short_name_fa']}{time_str}</b>",
        "",
    ]

    for idx, team_info in enumerate(parsed["teams"]):
        code = team_info["code"]
        team = _lookup_team(code, code)
        lines.append(f"<b>{team['short_name_fa']}</b>")
        lines.append("")

        for name in team_info["players"]:
            player = _resolve_player(name, code, "")
            if player:
                player_name = player["web_name_fa"] or player["web_name"]
                price = _price_display(player)
                lines.append(f"{player_name} {price}")
            else:
                lines.append(f"{name}")

        if idx < len(parsed["teams"]) - 1:
            lines.append("")
            lines.append("───────")
            lines.append("")

    lines.append("")
    lines.append("@EPL_Fantasy")
    return "\n".join(lines)
