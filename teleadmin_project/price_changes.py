"""Detect source price-change posts and render the channel's price-report layout."""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone


logger = logging.getLogger(__name__)

_HTML_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


_FA_DAYS = {
    0: "دوشنبه",
    1: "سه‌شنبه",
    2: "چهارشنبه",
    3: "پنجشنبه",
    4: "جمعه",
    5: "شنبه",
    6: "یکشنبه",
}


# Detection must stay tolerant of the source channel's formatting changes.
# It has posted both "Price Fallers! 📉 (3) #FPL" with "🔴 J.Timber #ARS £6.1m"
# rows and, later, "Price Fallers! 📉 #FPL" with "⬇ Madueke £6.3m" rows — no
# count, no team code. Requiring the count made the newer posts fall through to
# the LLM and get published as translated articles.
_DETECT_HEADER_RE = re.compile(
    r"^[^\w\n]*Price\s+(?:Risers|Fallers)\s*!", re.MULTILINE | re.IGNORECASE
)
_PRICE_ROW_RE = re.compile(r"£\s*\d+(?:\.\d+)?\s*m\b", re.IGNORECASE)


def _esc(text: str) -> str:
    return text.translate(_HTML_ESCAPES)



def is_price_change(text: str) -> bool:
    """Detect a source-channel price-change post so it is never translated.

    A leading header is accepted on its own, because these posts always open
    with it. Elsewhere in a message the header must be backed by real price
    rows, so an article that merely discusses risers and fallers in prose is
    still translated normally.
    """
    if not text:
        return False
    match = _DETECT_HEADER_RE.search(text)
    if not match:
        return False
    if not text[: match.start()].strip():
        return True
    return len(_PRICE_ROW_RE.findall(text)) >= 2



def persian_weekday(moment: datetime) -> str:
    """Return the Persian name of a moment's weekday."""
    return _FA_DAYS[moment.weekday()]


def _day_header() -> str:
    return f"تغییرات قیمت 💷 صبح {persian_weekday(datetime.now(tz=timezone.utc))} 🧐"



@dataclass(frozen=True)
class PriceMove:
    """One player's line in a price report, already resolved to Persian.

    Whoever produces the report — a relayed source post or the official FPL
    data — resolves the player themselves and hands over the finished pieces,
    so the layout below stays the one description of how a price post looks.
    """

    name: str
    flag: str = ""
    price: str = ""
    position: str = ""
    team: str = ""
    note: str = ""


def prediction_header() -> str:
    return "پیش‌بینی تغییرات قیمت امشب 💷 🧐"


def _move_line(move: PriceMove, arrow: str) -> str:
    flag = f"{move.flag} " if move.flag else " "
    price = f"<b>{_esc(move.price)}{_esc(move.position)}</b>" if move.price else ""
    note = f" (<b>{_esc(move.note)}</b>)" if move.note else ""
    team = f" ({_esc(move.team)})" if move.team else ""
    return f"<blockquote>{arrow} {_esc(move.name)}{flag}{price}{note}{team}</blockquote>"


def format_price_report(
    risers: list[PriceMove],
    fallers: list[PriceMove],
    *,
    header: str | None = None,
    empty_note: str = "",
    footer: bool = True,
) -> str:
    """Render the channel's price-report layout.

    This is the one place that layout lives. It was written to relay the
    source channel's price posts; the official FPL report renders through it
    too, so a reader sees the same post no matter which produced it. Keep new
    report kinds going through here rather than growing a second layout.
    """
    lines = [header if header is not None else _day_header(), ""]
    for arrow, heading, moves in (
        ("⬆️", "🔼 افزایش:", risers),
        ("⬇️", "🔽 کاهش:", fallers),
    ):
        if not moves:
            continue
        lines.extend([heading, ""])
        lines.extend(_move_line(move, arrow) for move in moves)
        lines.append("")
    if not risers and not fallers:
        if not empty_note:
            return ""
        lines.extend([empty_note, ""])
    if footer:
        lines.append("@EPL_Fantasy")
    return "\n".join(lines).rstrip("\n")



# ── Buffering: accumulate risers + fallers, post once both are received ──

