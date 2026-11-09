"""The gameweek fixtures post: which gameweek, its text, and when it is due.

Shared by the dashboard's /fixtures command and the scheduler's automatic
post, so both publish the same thing. Reads only the FPL database.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database as db
import price_changes

# The same picture as the deadline-passed post.
IMAGE = Path(__file__).parent / "deadline.jpg"

# Posted this long before the gameweek's deadline, once the previous
# gameweek's football is over.
LEAD_TIME = timedelta(hours=72)

_IRAN_OFFSET = timedelta(hours=3, minutes=30)
# A fixture still unfinished this long after kickoff was postponed, not played.
_FIXTURE_IN_PROGRESS_HOURS = 4


def _utc(value: str) -> datetime:
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def has_football_left(gameweek_id: int, now: datetime | None = None) -> bool:
    """Whether this gameweek still has a match to come or in progress.

    A fixture that has been unfinished for longer than a match can last was
    postponed rather than played, and must not hold a gameweek open until the
    rescheduled date.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=_FIXTURE_IN_PROGRESS_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bool(
        db.query_scalar(
            "SELECT count(*) FROM fixtures "
            "WHERE gameweek_id=? AND finished=0 AND kickoff_time > ?",
            (gameweek_id, cutoff),
        )
    )


def current_gameweek() -> dict:
    """Return the gameweek whose fixtures are worth posting now.

    The FPL API keeps a gameweek ``is_current`` until the *next* one's
    deadline, so between the final whistle and that deadline the command was
    still advertising matches that had already been played. Once the current
    gameweek has no football left in it, the next one is what people want.
    """
    current = db.query_one(
        "SELECT id, name, deadline_time FROM gameweeks WHERE is_current=1 ORDER BY id LIMIT 1"
    )
    if current:
        if has_football_left(current["id"]):
            return current
        upcoming = db.query_one(
            "SELECT g.id, g.name, g.deadline_time FROM gameweeks g "
            "JOIN fixtures f ON f.gameweek_id = g.id "
            "WHERE g.id > ? GROUP BY g.id ORDER BY g.id LIMIT 1",
            (current["id"],),
        )
        # Nothing follows the final gameweek of a season.
        return upcoming or current

    upcoming = db.query_one(
        "SELECT id, name, deadline_time FROM gameweeks WHERE is_next=1 ORDER BY id LIMIT 1"
    )
    if not upcoming:
        raise RuntimeError("گیم‌ویک فعلی یا بعدی پیدا نشد.")
    return upcoming


def due_post(now: datetime | None = None) -> tuple[dict, datetime] | None:
    """Return the gameweek to post fixtures for and when, or None if not yet.

    The post belongs to the next gameweek whose deadline is still ahead. It
    becomes due once the gameweek before it has no football left, and is
    meant to go out ``LEAD_TIME`` before the deadline; the caller schedules
    it for that moment, or sends it at once when that moment has passed.
    """
    now = now or datetime.now(timezone.utc)
    upcoming = [
        gw for gw in db.query("SELECT id, name, deadline_time FROM gameweeks ORDER BY id")
        if gw["deadline_time"] and _utc(gw["deadline_time"]) > now
    ]
    if not upcoming:
        return None
    gameweek = upcoming[0]
    if has_football_left(gameweek["id"] - 1, now):
        return None
    if not db.query_scalar("SELECT count(*) FROM fixtures WHERE gameweek_id=?", (gameweek["id"],)):
        return None
    return gameweek, _utc(gameweek["deadline_time"]) - LEAD_TIME


def text(gameweek: dict) -> str:
    """Render the fixtures post for one gameweek."""
    fixtures = db.query(
        """SELECT f.kickoff_time, ht.short_name_fa AS home_fa, ht.short_name AS home_en,
                  at.short_name_fa AS away_fa, at.short_name AS away_en
           FROM fixtures f
           JOIN teams ht ON ht.id=f.team_h JOIN teams at ON at.id=f.team_a
           WHERE f.gameweek_id=? ORDER BY f.kickoff_time, f.id""",
        (gameweek["id"],),
    )
    if not fixtures:
        raise RuntimeError("بازی‌ای برای این گیم‌ویک پیدا نشد.")

    deadline = _utc(gameweek["deadline_time"]) + _IRAN_OFFSET
    lines = [
        f"<b>⚠️ددلاین هفته {gameweek['id']}، "
        f"{price_changes.persian_weekday(deadline)} {deadline.strftime('%H:%M')}⚠️</b>",
        "",
    ]
    current_day = None
    for fixture in fixtures:
        kickoff = _utc(fixture["kickoff_time"]) + _IRAN_OFFSET
        # The post goes out weekly, so the weekday tells a reader which match
        # is which far quicker than a calendar date does.
        day = price_changes.persian_weekday(kickoff)
        if day != current_day:
            if current_day is not None:
                lines.append("")
            current_day = day
            lines.append(f"<b>{day}</b>")
        home = fixture["home_fa"] or fixture["home_en"]
        away = fixture["away_fa"] or fixture["away_en"]
        lines.append(f"<blockquote>{home} - {away} | <b>{kickoff.strftime('%H:%M')}</b></blockquote>")
    lines.extend(["", "@EPL_Fantasy"])
    return "\n".join(lines)
