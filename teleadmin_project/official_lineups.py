"""Fetch confirmed starting XIs from the Premier League match API."""
import logging
import re
import unicodedata

import requests

import alerts

logger = logging.getLogger(__name__)

_API_BASE = "https://sdp-prem-prod.premier-league-prod.pulselive.com"
_COMPETITION_ID = "8"  # Premier League
_REQUEST_TIMEOUT = 15

_match_id_cache: dict[int, dict] = {}


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _team_matches(team: dict, expected_name: str, expected_code: str) -> bool:
    expected_values = {_normalise(expected_name), _normalise(expected_code)}
    actual_values = {
        _normalise(team.get("name", "")),
        _normalise(team.get("shortName", "")),
        _normalise(team.get("abbr", "")),
    }
    return bool(expected_values & actual_values - {""})


def _season_id(kickoff_time: str) -> int:
    """Return the season's starting year for a UTC kickoff timestamp."""
    year = int(kickoff_time[:4])
    month = int(kickoff_time[5:7])
    return year if month >= 7 else year - 1


def _official_match(fixture: dict) -> dict | None:
    fixture_id = int(fixture["id"])
    cached_match = _match_id_cache.get(fixture_id)
    if cached_match:
        return cached_match

    season = _season_id(fixture["kickoff_time"])
    response = requests.get(
        f"{_API_BASE}/api/v2/matches",
        params={
            "competition": _COMPETITION_ID,
            "season": season,
            "matchweek": fixture["gameweek_id"],
            "_limit": 50,
        },
        headers={
            "Origin": "https://www.premierleague.com",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    matches = payload if isinstance(payload, list) else payload.get("data", [])

    for match in matches:
        home = match.get("homeTeam") or {}
        away = match.get("awayTeam") or {}
        if (
            _team_matches(home, fixture["home_en"], fixture["home_code"])
            and _team_matches(away, fixture["away_en"], fixture["away_code"])
        ):
            match_id = str(match.get("matchId") or match.get("id") or "")
            if match_id:
                _match_id_cache[fixture_id] = match
                return match

    logger.info(
        "Official match not found yet for %s vs %s (GW%s)",
        fixture["home_en"], fixture["away_en"], fixture["gameweek_id"],
    )
    return None


def _lookup_name(player: dict, team_code: str, other_team_code: str) -> str:
    """Resolve an official player to the FPL web name used by the DB."""
    candidates = [
        player.get("knownName", ""),
        player.get("lastName", ""),
        f"{player.get('firstName', '')} {player.get('lastName', '')}".strip(),
    ]
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        for value in (candidate, candidate.split()[-1]):
            resolved = alerts._resolve_player(
                value,
                team_code,
                other_team_code,
                strict_team_code=team_code,
            )
            if resolved:
                return resolved["web_name"]
    return (player.get("knownName") or player.get("lastName") or player.get("firstName") or "").strip()


def fetch_starting_lineups(fixture: dict) -> dict | None:
    """Return an alerts-compatible lineup containing starters only.

    An empty or partial official response is deliberately treated as not ready;
    the scheduler will retry it on its next 30-second pass.
    """
    match = _official_match(fixture)
    if not match:
        return None

    match_id = str(match.get("matchId") or match.get("id") or "")
    if not match_id:
        return None

    response = requests.get(
        f"{_API_BASE}/api/v2/matches/{match_id}/lineups",
        headers={
            "Origin": "https://www.premierleague.com",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return None

    by_team_id = {str(entry.get("teamId")): entry for entry in payload}
    home_entry = by_team_id.get(str((match.get("homeTeam") or {}).get("id")))
    away_entry = by_team_id.get(str((match.get("awayTeam") or {}).get("id")))
    if not home_entry or not away_entry:
        return None

    result = {
        "home_code": fixture["home_code"],
        "away_code": fixture["away_code"],
        "teams": [],
    }
    for entry, code, other_code in (
        (home_entry, fixture["home_code"], fixture["away_code"]),
        (away_entry, fixture["away_code"], fixture["home_code"]),
    ):
        players = {str(p.get("id")): p for p in entry.get("players", [])}
        starter_ids = [player_id for row in entry.get("lineup", []) for player_id in row]
        if len(starter_ids) != 11 or any(str(player_id) not in players for player_id in starter_ids):
            return None
        result["teams"].append(
            {
                "code": code,
                "players": [
                    _lookup_name(players[str(player_id)], code, other_code)
                    for player_id in starter_ids
                ],
            }
        )

    return result
