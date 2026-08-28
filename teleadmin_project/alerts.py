"""Parse English game-action alerts and format them in Farsi."""
import hashlib
import json
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
    r"(?<!\w)(Goal|Assist|Red card|Penalty missed|Penalty saved)\s*[-–—]\s*",
    re.IGNORECASE,
)

_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]")

_SCORE_RE = re.compile(
    r"^(.+?)\s+(\d+)\s*[-–—]\s*(\d+)\s+(.+?)\s*\((\d+(?:\+\d+)?)\s*(?:mins?|min)?\s*\)\s*$",
    re.MULTILINE,
)

_SCORE_SIMPLE_RE = re.compile(
    r"^(.+?)\s+(\d+)\s*[-–—]\s*(\d+)\s+(.+?)\s*$",
    re.MULTILINE,
)

# Some direct bot inputs arrive as one line rather than the line-broken
# format used by source channels. Keep the score body deliberately ASCII:
# source alerts use English team names, and this prevents action/player text
# from being mistaken for the home team.
_INLINE_SCORE_RE = re.compile(
    r"(?<!\S)(?=(?P<home>[A-Za-z][A-Za-z0-9 .&'’]*?)\s+"
    r"(?P<home_score>\d+)\s*[-–—]\s*(?P<away_score>\d+)\s+"
    r"(?P<away>[A-Za-z][A-Za-z0-9 .&'’]*?)"
    r"(?:\s*\((?P<minute>\d+(?:\+\d+)?)\s*(?:mins?|min)?\s*\))?"
    r"(?=\s*(?:#|\[|$)))",
    re.IGNORECASE,
)

_HASHTAG_RE = re.compile(r"#FPL\s*#(\w{3})(\w{3})", re.IGNORECASE)


def _strip_markdown_links(text: str) -> str:
    """Keep visible text from markdown links while dropping their URLs."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # X/Telegram copies can leave markdown escape slashes in plain text.
    return text.replace("\\", "")


def _find_score(text: str):
    """Find the score, including when actions and score share one line."""
    line_match = _SCORE_RE.search(text)
    if line_match:
        return line_match

    inline_matches = list(_INLINE_SCORE_RE.finditer(text))
    if inline_matches:
        # A preceding player name can also form a syntactically valid home
        # team candidate ("ENCISO Ipswich 1-0 ..."). The rightmost match is
        # the actual score because it starts at the real team name.
        return inline_matches[-1]
    return _SCORE_SIMPLE_RE.search(text)


@dataclass
class Action:
    type: str
    player_name: str
    detail: str | None = None
    team_code: str | None = None


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
    text = _strip_markdown_links(text)
    if _EMOJI_RE.search(text):
        return False
    action_matches = list(_ACTION_RE.finditer(text))
    action_names = {match.group(1).casefold() for match in action_matches}
    if "assist" in action_names and "goal" not in action_names:
        return False
    has_action = bool(_ACTION_RE.search(text))
    has_score = bool(_find_score(text))
    return has_action and has_score


def parse(text: str) -> ParsedAlert | None:
    text = _strip_markdown_links(text)
    if _EMOJI_RE.search(text):
        return None
    alert = ParsedAlert()

    score_m = _find_score(text)
    action_matches = list(_ACTION_RE.finditer(text))
    action_names = {match.group(1).casefold() for match in action_matches}
    if "assist" in action_names and "goal" not in action_names:
        return None
    for index, m in enumerate(action_matches):
        if score_m and m.start() >= score_m.start():
            continue

        next_action_start = (
            action_matches[index + 1].start()
            if index + 1 < len(action_matches)
            else len(text)
        )
        end = next_action_start
        if score_m and m.end() <= score_m.start() < end:
            end = score_m.start()

        action_type = m.group(1).capitalize()
        raw = re.sub(r"\s+", " ", text[m.end():end]).strip()
        if not raw:
            continue
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

    if score_m:
        if score_m.re is _INLINE_SCORE_RE:
            alert.home_team = score_m.group("home").strip()
            alert.home_score = int(score_m.group("home_score"))
            alert.away_score = int(score_m.group("away_score"))
            alert.away_team = score_m.group("away").strip()
            alert.minute = score_m.group("minute") or "?"
        else:
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


def dedup_key(alert: ParsedAlert) -> str:
    """Return a stable identity for the same match action across source feeds.

    The minute is intentionally excluded because different feeds can report
    the same action a few seconds apart with different stoppage-time text.
    """
    def value(text: str) -> str:
        return re.sub(r"\s+", " ", _normalize(text or "")).strip().casefold()

    payload = {
        "home": value(alert.home_team) or value(alert.home_team_code),
        "away": value(alert.away_team) or value(alert.away_team_code),
        "home_score": alert.home_score,
        "away_score": alert.away_score,
        "actions": sorted(
            (value(action.type), value(action.player_name), value(action.detail or ""))
            for action in alert.actions
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _resolve_player(
    name: str,
    home_team_code: str,
    away_team_code: str,
    strict_team_code: str | None = None,
    allowed_team_codes: tuple[str, ...] | None = None,
) -> dict | None:
    """Resolve a player, optionally restricting candidates to fixture clubs."""
    normalized = db.normalize_player_name(name)
    if not normalized:
        return None
    # Fetch the small relevant club set first, then normalize in Python.
    # SQLite cannot perform accent-insensitive comparisons reliably.
    where = "1 = 1"
    params: list[str] = []
    if strict_team_code:
        # Lineup feeds identify the club for every player.  Restricting the
        # candidate set prevents a same-name player at another club from
        # winning before the team-priority ordering is even applied.
        where += " AND upper(t.short_name) = upper(?)"
        params.append(strict_team_code)
    elif allowed_team_codes:
        codes = tuple(code for code in allowed_team_codes if code)
        if codes:
            where += " AND upper(t.short_name) IN (" + ",".join("?" for _ in codes) + ")"
            params.extend(codes)
    params.extend((home_team_code, away_team_code))
    results = db.query(
        f"""SELECT players.*, pos.singular_name AS pos_name, t.short_name AS team_code,
                  t.name_fa, t.short_name_fa
           FROM players
           JOIN positions pos ON players.position_id = pos.id
           JOIN teams t ON players.team_id = t.id
           WHERE {where}
           ORDER BY
             CASE WHEN t.short_name IN (?, ?) THEN 0 ELSE 1 END,
             total_points DESC""",
        tuple(params),
    )
    for player in results:
        if db.player_name_matches(player, name, allow_partial=True):
            return player
    return None


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
    # Hashtags normally provide the compact team codes. If a direct/manual
    # alert omits them, infer the same codes from the two named clubs before
    # resolving any player so the lookup remains fixture-scoped.
    for field, team_name in (
        ("home_team_code", alert.home_team),
        ("away_team_code", alert.away_team),
    ):
        if not getattr(alert, field) and team_name:
            team = db.query_one(
                "SELECT short_name FROM teams WHERE lower(name) = lower(?) LIMIT 1",
                (team_name,),
            )
            if team:
                setattr(alert, field, team["short_name"])
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
                strict_team_code=action.team_code,
                allowed_team_codes=(alert.home_team_code, alert.away_team_code),
            )
            if not player:
                lines.append(
                    f"\u26bd {_esc(action.player_name) if action.player_name else _esc('در انتظار تأیید')} "
                    f"({_esc('گل بخودی')})"
                )
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
                strict_team_code=action.team_code,
                allowed_team_codes=(alert.home_team_code, alert.away_team_code),
            )
            if not player:
                lines.append(
                    f"\u26bd {_esc(action.player_name) if action.player_name else _esc('در انتظار تأیید')}"
                )
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
                    strict_team_code=action.team_code,
                    allowed_team_codes=(alert.home_team_code, alert.away_team_code),
                )
                if not player:
                    lines.append(
                        f"\U0001f170\ufe0f {_esc(action.player_name) if action.player_name else _esc('در انتظار تأیید')}"
                    )
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
                strict_team_code=action.team_code,
                allowed_team_codes=(alert.home_team_code, alert.away_team_code),
            )
            if not player:
                lines.append(
                    f"\u2666 {_esc('اخراج')} {_esc(action.player_name)}"
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
                strict_team_code=action.team_code,
                allowed_team_codes=(alert.home_team_code, alert.away_team_code),
            )
            if not player:
                lines.append(f"\u274c {_esc(action.player_name)}")
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
                strict_team_code=action.team_code,
                allowed_team_codes=(alert.home_team_code, alert.away_team_code),
            )
            if not player:
                lines.append(f"\U0001f4db {_esc(action.player_name)}")
            else:
                lines.append(
                    f"\U0001f4db {player['web_name_fa'] or player['web_name']} "
                    f"{_price_display(player)} ({_esc('مهار پنالتی')})"
                )

    lines.append("")
    lines.append("@EPL_Fantasy")
    return "\n".join(lines)


def format_provisional_goal(
    *,
    home_team: str,
    away_team: str,
    home_code: str,
    away_code: str,
    home_score: int,
    away_score: int,
    scorer_name: str = "",
    scorer_team_code: str = "",
    own_goal: bool = False,
) -> str:
    """Format a fast API goal post before the source alert is confirmed.

    The API can identify a scorer before it exposes a reliable assister.  The
    assister therefore remains explicitly pending and the whole message can
    later be replaced with the confirmed source version.
    """
    alert = ParsedAlert(
        actions=[
            Action("own_goal" if own_goal else "Goal", scorer_name, team_code=scorer_team_code or None),
            Action("Assist", "tbd"),
        ],
        home_team=home_team,
        away_team=away_team,
        home_score=int(home_score),
        away_score=int(away_score),
        minute="?",
        home_team_code=home_code,
        away_team_code=away_code,
    )
    return format_farsi(alert) or ""


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


def lineup_dedup_key(parsed: dict, day: str) -> str:
    """Identify one fixture's lineup across source feeds and the API watcher."""
    return (
        f"automatic_lineup_{day}_"
        f"{parsed.get('home_code', '').upper()}_{parsed.get('away_code', '').upper()}"
    )


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
            player = _resolve_player(name, code, "", strict_team_code=code)
            if player:
                player_name = player["web_name_fa"] or player["web_name"]
                price = _price_display(player)
                lines.append(f"<blockquote>{player_name} {price}</blockquote>")
            else:
                lines.append(f"<blockquote>{name}</blockquote>")

        if idx < len(parsed["teams"]) - 1:
            lines.append("")
            lines.append("───────")
            lines.append("")

    lines.append("")
    lines.append("@EPL_Fantasy")
    return "\n".join(lines)
