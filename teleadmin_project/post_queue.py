"""Half-hour publishing-slot allocation for translated posts."""
from datetime import datetime, time, timedelta, timezone
from typing import Iterable

IRAN_TZ = timezone(timedelta(hours=3, minutes=30))
_MORNING_FIRST = time(8, 30)
_OVERNIGHT_START = time(0, 30)


def _slot_key(value: datetime) -> tuple[int, int, int, int, int]:
    local = value.astimezone(IRAN_TZ)
    return local.year, local.month, local.day, local.hour, local.minute


def _is_allowed_slot(value: datetime) -> bool:
    local_time = value.astimezone(IRAN_TZ).time().replace(tzinfo=None)
    if local_time.minute not in {0, 30}:
        return False
    return local_time <= _OVERNIGHT_START or local_time >= _MORNING_FIRST


def _next_allowed_slot(value: datetime) -> datetime:
    """Return the first allowed half-hour slot strictly after ``value``."""
    local = value.astimezone(IRAN_TZ)
    candidate = local.replace(second=0, microsecond=0)
    candidate = candidate.replace(minute=30 if candidate.minute < 30 else 0)
    if local.minute >= 30:
        candidate += timedelta(hours=1)
    elif local.minute == 0 and local.second == 0 and local.microsecond == 0:
        candidate = local + timedelta(minutes=30)
    # The normalization above is intentionally simple; this loop also crosses
    # the 00:30–08:30 publishing blackout without special date arithmetic.
    while not _is_allowed_slot(candidate):
        candidate += timedelta(minutes=30)
    return candidate


def first_candidate(now: datetime) -> datetime:
    """Return the queue's first candidate, skipping the current upcoming slot.

    During the overnight blackout, 08:30 is used directly rather than skipped.
    This keeps posts received from 00:30 through 08:00 at the front of the
    morning queue.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(IRAN_TZ)
    local_time = local.time().replace(tzinfo=None)
    if _OVERNIGHT_START <= local_time <= time(8, 0):
        return local.replace(hour=8, minute=30, second=0, microsecond=0)
    current_upcoming = _next_allowed_slot(local)
    return _next_allowed_slot(current_upcoming)


def slot_at_or_after(value: datetime) -> datetime:
    """Return the half-hour boundary at or after ``value``, in UTC.

    Unlike ``_next_allowed_slot`` this ignores the overnight blackout: it is
    for working out when something else will happen, not where a post goes.
    """
    value = value.astimezone(timezone.utc)
    floor = value.replace(minute=0 if value.minute < 30 else 30, second=0, microsecond=0)
    return floor if floor == value else floor + timedelta(minutes=30)


def next_available_slot(now: datetime, occupied: Iterable[datetime] = ()) -> datetime:
    """Return the first slot from now on that nothing occupies.

    Only ``occupied`` decides what is taken. A slot freed because a scheduled
    post was published early or deleted is filled again by the next post,
    rather than the queue resuming after the last slot it once used.
    """
    candidate = first_candidate(now)
    occupied_keys = {_slot_key(value) for value in occupied if value.tzinfo is not None}
    while _slot_key(candidate) in occupied_keys:
        candidate = _next_allowed_slot(candidate)
    return candidate.astimezone(timezone.utc)
