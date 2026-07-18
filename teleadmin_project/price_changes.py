"""Parse English price-change alerts, accumulate risers + fallers, format in Farsi."""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import database as db

logger = logging.getLogger(__name__)

_HTML_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})

_POS_LETTER = {"Goalkeeper": "G", "Defender": "D", "Midfielder": "M", "Forward": "F"}

_FA_DAYS = {
    0: "دوشنبه",
    1: "سه‌شنبه",
    2: "چهارشنبه",
    3: "پنجشنبه",
    4: "جمعه",
    5: "شنبه",
    6: "یکشنبه",
}

_HEADER_RE = re.compile(
    r"^Price (Risers|Fallers)!.*?\((\d+)\).*$", re.MULTILINE | re.IGNORECASE
)


def _esc(text: str) -> str:
    return text.translate(_HTML_ESCAPES)


@dataclass
class PriceChange:
    player_name: str
    team_code: str
    new_price_raw: str


@dataclass
class ParsedPriceChange:
    change_type: str  # "risers" or "fallers"
    count: int
    players: list[PriceChange] = field(default_factory=list)


def is_price_change(text: str) -> bool:
    if not text:
        return False
    return bool(_HEADER_RE.search(text))


def parse_price_change(text: str) -> ParsedPriceChange | None:
    header_m = _HEADER_RE.search(text)
    if not header_m:
        return None

    change_type = "risers" if header_m.group(1).lower() == "risers" else "fallers"
    count = int(header_m.group(2))
    emoji = "🟢" if change_type == "risers" else "🔴"

    players = []
    for m in re.finditer(
        rf"^{emoji}\s*(.+?)\s*#(\w{{3}})\s*£([\d.]+)m?$",
        text,
        re.MULTILINE,
    ):
        players.append(
            PriceChange(
                player_name=m.group(1).strip(),
                team_code=m.group(2).upper(),
                new_price_raw=m.group(3),
            )
        )

    return ParsedPriceChange(change_type=change_type, count=count, players=players)


def _day_header() -> str:
    now = datetime.now(tz=timezone.utc)
    day_name = _FA_DAYS[now.weekday()]
    return f"تغییرات قیمت 💷 صبح {day_name} 🧐"


def _resolve_player(name: str, team_code: str) -> dict | None:
    import unicodedata

    def normalize(text):
        return "".join(
            c
            for c in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(c)
        )

    norm = normalize(name).lower()
    results = db.query(
        """SELECT players.*, pos.singular_name AS pos_name,
                  t.short_name AS team_code, t.short_name_fa
           FROM players
           JOIN positions pos ON players.position_id = pos.id
           JOIN teams t ON players.team_id = t.id
           WHERE lower(alias) = ?
              OR t.short_name = ?
           ORDER BY total_points DESC""",
        (norm, team_code),
    )

    # Filter in Python for accent-insensitive matching
    for player in results:
        if normalize(player["web_name"]).lower() == norm:
            return player
        full_name = normalize(
            f"{player['first_name']} {player['second_name']}"
        ).lower()
        if norm in full_name or norm == normalize(player["search_name"]).lower():
            return player

    return None


def _price_display(player: dict | None, source_price: str) -> str:
    pos_letter = _POS_LETTER.get(player["pos_name"], "?") if player else "?"
    return f"<b>{source_price}{pos_letter}</b>"


def format_price_changes_farsi(risers: list[PriceChange], fallers: list[PriceChange]) -> str:
    lines = [_day_header(), ""]

    if risers:
        lines.append("🔼 افزایش:")
        lines.append("")
        for pc in risers:
            player = _resolve_player(pc.player_name, pc.team_code)
            if player:
                name = player["web_name_fa"] or player["web_name"]
                price = _price_display(player, pc.new_price_raw)
                flag = (player.get("flag") or "") + " "
                team = db.query_one(
                    "SELECT short_name_fa FROM teams WHERE short_name=?",
                    (pc.team_code,),
                )
                team_str = team["short_name_fa"] if team else pc.team_code
                lines.append(f"<blockquote>⬆️ {name}{flag}{price} ({team_str})</blockquote>")
            else:
                lines.append(f"<blockquote>⬆️ {pc.player_name} £{pc.new_price_raw}m ({pc.team_code})</blockquote>")
        lines.append("")

    if fallers:
        lines.append("🔽 کاهش:")
        lines.append("")
        for pc in fallers:
            player = _resolve_player(pc.player_name, pc.team_code)
            if player:
                name = player["web_name_fa"] or player["web_name"]
                price = _price_display(player, pc.new_price_raw)
                flag = (player.get("flag") or "") + " "
                team = db.query_one(
                    "SELECT short_name_fa FROM teams WHERE short_name=?",
                    (pc.team_code,),
                )
                team_str = team["short_name_fa"] if team else pc.team_code
                lines.append(f"<blockquote>⬇️ {name}{flag}{price} ({team_str})</blockquote>")
            else:
                lines.append(f"<blockquote>⬇️ {pc.player_name} £{pc.new_price_raw}m ({pc.team_code})</blockquote>")
        lines.append("")

    lines.append("@EPL_Fantasy")
    return "\n".join(lines)


# ── Buffering: accumulate risers + fallers, post once both are received ──

_pending: dict[str, dict] = {}
_pending_tasks: dict[str, asyncio.Task] = {}
_BUFFER_SECONDS = 120


async def _delayed_post(date_key: str, callback):
    await asyncio.sleep(_BUFFER_SECONDS)
    data = _pending.pop(date_key, None)
    _pending_tasks.pop(date_key, None)
    if data:
        risers = data.get("risers", [])
        fallers = data.get("fallers", [])
        await callback(risers, fallers)


def accumulate(parsed: ParsedPriceChange, callback) -> str | None:
    from datetime import date

    today = date.today().isoformat()

    if today not in _pending:
        _pending[today] = {"risers": [], "fallers": []}

    if parsed.change_type == "risers":
        _pending[today]["risers"].extend(parsed.players)
    else:
        _pending[today]["fallers"].extend(parsed.players)

    data = _pending.get(today, {})
    has_risers = bool(data.get("risers"))
    has_fallers = bool(data.get("fallers"))

    if has_risers and has_fallers:
        risers = data.pop("risers")
        fallers = data.pop("fallers")
        _pending.pop(today, None)
        task = _pending_tasks.pop(today, None)
        if task:
            task.cancel()
        return format_price_changes_farsi(risers, fallers)

    if today not in _pending_tasks:
        _pending_tasks[today] = asyncio.create_task(
            _delayed_post(today, callback)
        )

    return None
