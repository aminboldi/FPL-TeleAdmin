import json
import logging
import os
import requests
import shutil
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).parent / "fpl.db"
_RUNTIME_CONFIG_PATH = os.getenv("RUNTIME_CONFIG_PATH")
DB_PATH = Path(
    os.getenv("FPL_DATABASE_PATH")
    or (
        Path(_RUNTIME_CONFIG_PATH).parent / "fpl.db"
        if _RUNTIME_CONFIG_PATH
        else _DEFAULT_DB_PATH
    )
)
_FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
_FPL_FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gameweeks (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    deadline_time   TEXT    NOT NULL,
    finished        INTEGER NOT NULL DEFAULT 0,
    is_current      INTEGER NOT NULL DEFAULT 0,
    is_next         INTEGER NOT NULL DEFAULT 0,
    average_entry_score INTEGER,
    highest_score   INTEGER
);

CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    short_name      TEXT    NOT NULL,
    strength        INTEGER,
    strength_overall_home INTEGER,
    strength_overall_away INTEGER,
    name_fa         TEXT,
    short_name_fa   TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY,
    singular_name   TEXT    NOT NULL,
    squad_select    INTEGER,
    squad_min_play  INTEGER
);

CREATE TABLE IF NOT EXISTS players (
    id              INTEGER PRIMARY KEY,
    first_name      TEXT    NOT NULL,
    second_name     TEXT    NOT NULL,
    web_name        TEXT    NOT NULL,
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    position_id     INTEGER NOT NULL REFERENCES positions(id),
    now_cost        INTEGER NOT NULL,
    selected_by_percent TEXT,
    form            TEXT,
    total_points    INTEGER,
    ep_next         TEXT,
    ep_this         TEXT,
    event_points    INTEGER,
    minutes         INTEGER,
    goals_scored    INTEGER,
    assists         INTEGER,
    clean_sheets    INTEGER,
    goals_conceded  INTEGER,
    yellow_cards    INTEGER,
    red_cards       INTEGER,
    bonus           INTEGER,
    bps             INTEGER,
    influence       TEXT,
    creativity      TEXT,
    threat          TEXT,
    ict_index       TEXT,
    expected_goals          TEXT,
    expected_assists        TEXT,
    expected_goal_involvements TEXT,
    expected_goals_conceded TEXT,
    cost_change_event       INTEGER,
    cost_change_start       INTEGER,
    status          TEXT,
    news            TEXT,
    chance_of_playing_next_round INTEGER,
    first_name_fa   TEXT,
    second_name_fa  TEXT,
    web_name_fa     TEXT,
    alias           TEXT,
    search_name     TEXT,
    region          INTEGER,
    flag            TEXT
);

CREATE TABLE IF NOT EXISTS fixtures (
    id              INTEGER PRIMARY KEY,
    gameweek_id     INTEGER NOT NULL REFERENCES gameweeks(id),
    team_h          INTEGER NOT NULL REFERENCES teams(id),
    team_a          INTEGER NOT NULL REFERENCES teams(id),
    team_h_score    INTEGER,
    team_a_score    INTEGER,
    finished        INTEGER NOT NULL DEFAULT 0,
    kickoff_time    TEXT    NOT NULL,
    minutes         INTEGER,
    team_h_difficulty INTEGER,
    team_a_difficulty INTEGER
);

CREATE TABLE IF NOT EXISTS last_updated (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_map (
    source_chat_id  INTEGER NOT NULL,
    source_msg_id   INTEGER NOT NULL,
    target_msg_id   INTEGER NOT NULL,
    PRIMARY KEY (source_chat_id, source_msg_id)
);

-- Fast live-goal posts are created before a source alert is available.  Keep
-- their target message IDs so later API/source updates can edit the same post
-- instead of creating duplicates.
CREATE TABLE IF NOT EXISTS goal_alerts (
    goal_key        TEXT PRIMARY KEY,
    fixture_id      INTEGER NOT NULL,
    target_channel  TEXT NOT NULL,
    target_msg_id   INTEGER NOT NULL,
    home_code       TEXT NOT NULL,
    away_code       TEXT NOT NULL,
    home_score      INTEGER NOT NULL,
    away_score      INTEGER NOT NULL,
    scoring_side    TEXT,
    side_goal_no    INTEGER,
    text            TEXT NOT NULL,
    confirmed       INTEGER NOT NULL DEFAULT 0,
    scorer_id       INTEGER,
    scorer_kind     TEXT,
    cancelled       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_goal_alerts_fixture
    ON goal_alerts(fixture_id, home_score, away_score);

CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_id);
CREATE INDEX IF NOT EXISTS idx_players_position ON players(position_id);
CREATE INDEX IF NOT EXISTS idx_players_form ON players(form);
CREATE INDEX IF NOT EXISTS idx_players_search ON players(search_name);
CREATE INDEX IF NOT EXISTS idx_fixtures_gameweek ON fixtures(gameweek_id);
CREATE INDEX IF NOT EXISTS idx_fixtures_teams ON fixtures(team_h, team_a);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH != _DEFAULT_DB_PATH and not DB_PATH.exists() and _DEFAULT_DB_PATH.exists():
        shutil.copy2(_DEFAULT_DB_PATH, DB_PATH)
        logger.info("Initialized persistent FPL database from bundled database at %s", DB_PATH)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # Additive migration for databases created before VAR-cancellation
        # tracking was introduced.
        try:
            conn.execute(
                "ALTER TABLE goal_alerts ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    logger.info("Database schema initialized at %s", DB_PATH)


def import_bootstrap(json_path: str) -> None:
    with open(json_path) as f:
        data = json.load(f)

    with _connect() as conn:
        _upsert_gameweeks(conn, data.get("events", []))
        _upsert_teams(conn, data.get("teams", []))
        _upsert_positions(conn, data.get("element_types", []))
        _upsert_players(conn, data.get("elements", []))
        _set_updated(conn, "bootstrap")


def import_fixtures(json_path: str) -> None:
    with open(json_path) as f:
        fixtures = json.load(f)

    with _connect() as conn:
        _upsert_fixtures(conn, fixtures)
        _set_updated(conn, "fixtures")


def refresh_from_fpl_api(timeout: int = 25) -> dict[str, Any]:
    """Refresh official FPL data while preserving local manual enrichments."""
    bootstrap_response = requests.get(_FPL_BOOTSTRAP_URL, timeout=timeout)
    bootstrap_response.raise_for_status()
    bootstrap = bootstrap_response.json()
    if not isinstance(bootstrap, dict) or not isinstance(bootstrap.get("elements"), list):
        raise ValueError("FPL bootstrap response has an unexpected shape")

    fixtures_response = requests.get(_FPL_FIXTURES_URL, timeout=timeout)
    fixtures_response.raise_for_status()
    fixtures = fixtures_response.json()
    if not isinstance(fixtures, list):
        raise ValueError("FPL fixtures response has an unexpected shape")

    incoming_players = bootstrap["elements"]
    incoming_player_ids = {row.get("id") for row in incoming_players}
    with _connect() as conn:
        conn.executescript(SCHEMA)
        existing_ids = {
            row[0] for row in conn.execute("SELECT id FROM players").fetchall()
        }
        _upsert_gameweeks(conn, bootstrap.get("events", []))
        _upsert_teams(conn, bootstrap.get("teams", []))
        _upsert_positions(conn, bootstrap.get("element_types", []))
        _upsert_players(conn, incoming_players)
        _upsert_fixtures(conn, fixtures)
        _set_updated(conn, "bootstrap")
        _set_updated(conn, "fixtures")
        _set_updated(conn, "database_refresh")

    new_players = [
        {
            "id": row.get("id"),
            "first_name": row.get("first_name", ""),
            "second_name": row.get("second_name", ""),
            "web_name": row.get("web_name", ""),
        }
        for row in incoming_players
        if row.get("id") not in existing_ids
    ]
    return {
        "player_count": len(incoming_player_ids),
        "new_players": new_players,
        "fixture_count": len(fixtures),
    }


def _upsert_gameweeks(conn: sqlite3.Connection, rows: list[dict]) -> None:
    sql = """
    INSERT INTO gameweeks (id, name, deadline_time, finished, is_current, is_next,
                           average_entry_score, highest_score)
    VALUES (:id, :name, :deadline_time, :finished, :is_current, :is_next,
            :average_entry_score, :highest_score)
    ON CONFLICT(id) DO UPDATE SET
        name=excluded.name, deadline_time=excluded.deadline_time,
        finished=excluded.finished, is_current=excluded.is_current,
        is_next=excluded.is_next, average_entry_score=excluded.average_entry_score,
        highest_score=excluded.highest_score
    """
    conn.executemany(sql, rows)
    logger.info("Upserted %d gameweeks", len(rows))


def _upsert_teams(conn: sqlite3.Connection, rows: list[dict]) -> None:
    sql = """
    INSERT INTO teams (id, name, short_name, strength,
                       strength_overall_home, strength_overall_away)
    VALUES (:id, :name, :short_name, :strength,
            :strength_overall_home, :strength_overall_away)
    ON CONFLICT(id) DO UPDATE SET
        name=excluded.name, short_name=excluded.short_name,
        strength=excluded.strength,
        strength_overall_home=excluded.strength_overall_home,
        strength_overall_away=excluded.strength_overall_away
    """
    conn.executemany(sql, rows)
    logger.info("Upserted %d teams", len(rows))


def _upsert_positions(conn: sqlite3.Connection, rows: list[dict]) -> None:
    sql = """
    INSERT INTO positions (id, singular_name, squad_select, squad_min_play)
    VALUES (:id, :singular_name, :squad_select, :squad_min_play)
    ON CONFLICT(id) DO UPDATE SET
        singular_name=excluded.singular_name,
        squad_select=excluded.squad_select,
        squad_min_play=excluded.squad_min_play
    """
    conn.executemany(sql, rows)
    logger.info("Upserted %d positions", len(rows))


_PLAYER_COLS = [
    "first_name", "second_name", "web_name",
    "now_cost", "selected_by_percent", "form", "total_points",
    "ep_next", "ep_this", "event_points", "minutes",
    "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "yellow_cards", "red_cards", "bonus", "bps",
    "influence", "creativity", "threat", "ict_index",
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "expected_goals_conceded", "cost_change_event", "cost_change_start",
    "status", "news", "chance_of_playing_next_round",
    "region",
]

_PLAYER_SQL = f"""
INSERT INTO players (id, team_id, position_id, {", ".join(_PLAYER_COLS)}, search_name, flag)
VALUES (:id, :team, :element_type, {", ".join(":" + c for c in _PLAYER_COLS)}, :search_name, :flag)
ON CONFLICT(id) DO UPDATE SET
    team_id=excluded.team_id, position_id=excluded.position_id,
    {", ".join(f"{c}=excluded.{c}" for c in _PLAYER_COLS)},
    search_name=excluded.search_name,
    flag=excluded.flag
"""


def _normalize(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def alias_matches(alias: str | None, name: str) -> bool:
    """Match one incoming name against comma-separated manual aliases."""
    target = _normalize(str(name or "")).strip().casefold()
    if not target:
        return False
    return any(
        _normalize(part).strip().casefold() == target
        for part in str(alias or "").split(",")
        if part.strip()
    )


def _region_to_flag(region_id: int | None) -> str:
    if not region_id:
        return ""
    _load_region_map()
    return _REGION_FLAG_MAP.get(region_id, "")


_REGION_FLAG_MAP: dict[int, str] | None = None


def _load_region_map() -> None:
    global _REGION_FLAG_MAP
    if _REGION_FLAG_MAP is not None:
        return

    BLACK_FLAG = chr(0x1F3F4)
    CANCEL_TAG = chr(0xE007F)
    TAG_A = 0xE0061

    def _subdiv_flag(tag_str: str) -> str:
        def _tag(c):
            return chr(TAG_A + ord(c) - ord("a"))
        return BLACK_FLAG + "".join(_tag(c) for c in tag_str) + CANCEL_TAG

    SUBDIV_MAP = {"ENG": "gbeng", "SCO": "gbsct", "WAL": "gbwls"}

    _REGION_FLAG_MAP = {}
    try:
        import json as _json
        with open(Path(__file__).parent / "regions.json") as f:
            regions = _json.load(f)
        for r in regions:
            long_code = r.get("iso_code_long", "")
            if long_code in SUBDIV_MAP:
                flag = _subdiv_flag(SUBDIV_MAP[long_code])
            else:
                iso = r.get("iso_code_short", "")
                if len(iso) == 2 and iso.isalpha():
                    flag = chr(ord(iso[0]) + 0x1F1A5) + chr(ord(iso[1]) + 0x1F1A5)
                else:
                    flag = ""
            _REGION_FLAG_MAP[r["id"]] = flag
    except FileNotFoundError:
        pass


def _upsert_players(conn: sqlite3.Connection, rows: list[dict]) -> None:
    _load_region_map()
    for row in rows:
        row["search_name"] = _normalize(row["second_name"])
        row["flag"] = _REGION_FLAG_MAP.get(row.get("region"), "") if _REGION_FLAG_MAP else ""
    conn.executemany(_PLAYER_SQL, rows)
    logger.info("Upserted %d players", len(rows))


def _upsert_fixtures(conn: sqlite3.Connection, rows: list[dict]) -> None:
    sql = """
    INSERT INTO fixtures (id, gameweek_id, team_h, team_a, team_h_score,
                          team_a_score, finished, kickoff_time, minutes,
                          team_h_difficulty, team_a_difficulty)
    VALUES (:id, :event, :team_h, :team_a, :team_h_score,
            :team_a_score, :finished, :kickoff_time, :minutes,
            :team_h_difficulty, :team_a_difficulty)
    ON CONFLICT(id) DO UPDATE SET
        gameweek_id=excluded.gameweek_id,
        team_h=excluded.team_h, team_a=excluded.team_a,
        team_h_score=excluded.team_h_score,
        team_a_score=excluded.team_a_score,
        finished=excluded.finished, kickoff_time=excluded.kickoff_time,
        minutes=excluded.minutes,
        team_h_difficulty=excluded.team_h_difficulty,
        team_a_difficulty=excluded.team_a_difficulty
    """
    conn.executemany(sql, rows)
    logger.info("Upserted %d fixtures", len(rows))


def _set_updated(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "INSERT INTO last_updated (key, value) VALUES (?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=datetime('now')",
        (key,),
    )


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def query_scalar(sql: str, params: tuple = ()) -> Any:
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None


def list_players_for_edit() -> list[dict]:
    """Return the player name fields used by the protected web editor."""
    return query(
        """
        SELECT id, first_name, second_name, web_name, alias,
               first_name_fa, second_name_fa, web_name_fa
        FROM players
        ORDER BY web_name COLLATE NOCASE,
                 second_name COLLATE NOCASE,
                 first_name COLLATE NOCASE,
                 id
        """
    )


def update_player_farsi_names(
    updates: list[tuple[int, str | None, str | None, str | None, str | None]],
) -> int:
    """Update Persian names and comma-separated aliases in one transaction."""
    if not updates:
        return 0

    normalized = []
    seen_ids = set()
    for player_id, first_name_fa, second_name_fa, web_name_fa, alias in updates:
        player_id = int(player_id)
        if player_id in seen_ids:
            raise ValueError("duplicate player id")
        seen_ids.add(player_id)

        def clean(value):
            value = str(value or "").strip()
            return value or None

        aliases = []
        seen_aliases = set()
        for item in str(alias or "").split(","):
            item = item.strip()
            key = _normalize(item).casefold()
            if item and key not in seen_aliases:
                aliases.append(item)
                seen_aliases.add(key)

        normalized.append(
            (
                clean(first_name_fa),
                clean(second_name_fa),
                clean(web_name_fa),
                ", ".join(aliases) or None,
                player_id,
            )
        )

    with _connect() as conn:
        for values in normalized:
            result = conn.execute(
                """
                UPDATE players
                SET first_name_fa = ?, second_name_fa = ?, web_name_fa = ?, alias = ?
                WHERE id = ?
                """,
                values,
            )
            if result.rowcount != 1:
                raise ValueError(f"unknown player id: {values[-1]}")
    return len(normalized)


def get_db_path() -> Path:
    return DB_PATH


def store_message_mapping(source_chat_id: int, source_msg_id: int, target_msg_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO message_map (source_chat_id, source_msg_id, target_msg_id) "
            "VALUES (?, ?, ?)",
            (source_chat_id, source_msg_id, target_msg_id),
        )


def lookup_target_msg(source_chat_id: int, source_msg_id: int) -> int | None:
    return query_scalar(
        "SELECT target_msg_id FROM message_map WHERE source_chat_id = ? AND source_msg_id = ?",
        (source_chat_id, source_msg_id),
    )


def store_goal_alert(
    goal_key: str,
    fixture_id: int,
    target_channel: str,
    target_msg_id: int,
    home_code: str,
    away_code: str,
    home_score: int,
    away_score: int,
    scoring_side: str | None,
    side_goal_no: int | None,
    text: str,
    *,
    confirmed: bool = False,
    scorer_id: int | None = None,
    scorer_kind: str | None = None,
) -> None:
    """Persist one live-goal target message and its current API/source state."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO goal_alerts (
                goal_key, fixture_id, target_channel, target_msg_id,
                home_code, away_code, home_score, away_score, scoring_side,
                side_goal_no, text, confirmed, scorer_id, scorer_kind, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(goal_key) DO UPDATE SET
                target_channel=excluded.target_channel,
                target_msg_id=excluded.target_msg_id,
                home_code=excluded.home_code,
                away_code=excluded.away_code,
                home_score=excluded.home_score,
                away_score=excluded.away_score,
                scoring_side=excluded.scoring_side,
                side_goal_no=excluded.side_goal_no,
                text=excluded.text,
                confirmed=excluded.confirmed,
                scorer_id=excluded.scorer_id,
                scorer_kind=excluded.scorer_kind,
                updated_at=datetime('now')
            """,
            (
                goal_key,
                int(fixture_id),
                str(target_channel),
                int(target_msg_id),
                str(home_code),
                str(away_code),
                int(home_score),
                int(away_score),
                scoring_side,
                side_goal_no,
                text,
                int(bool(confirmed)),
                scorer_id,
                scorer_kind,
            ),
        )


def get_goal_alert(goal_key: str) -> dict | None:
    return query_one("SELECT * FROM goal_alerts WHERE goal_key = ?", (goal_key,))


def find_goal_alert(
    home_code: str,
    away_code: str,
    home_score: int,
    away_score: int,
) -> dict | None:
    """Find the latest provisional/confirmed post for one match scoreline."""
    return query_one(
        """
        SELECT * FROM goal_alerts
        WHERE upper(home_code) = upper(?) AND upper(away_code) = upper(?)
          AND home_score = ? AND away_score = ?
        ORDER BY updated_at DESC, goal_key DESC
        LIMIT 1
        """,
        (home_code, away_code, int(home_score), int(away_score)),
    )


def list_pending_goal_alerts(fixture_id: int) -> list[dict]:
    return query(
        """
        SELECT * FROM goal_alerts
        WHERE fixture_id = ? AND confirmed = 0 AND cancelled = 0
        ORDER BY home_score, away_score, goal_key
        """,
        (int(fixture_id),),
    )


def list_goal_alerts(fixture_id: int) -> list[dict]:
    return query(
        "SELECT * FROM goal_alerts WHERE fixture_id = ? ORDER BY home_score, away_score, goal_key",
        (int(fixture_id),),
    )


def update_goal_alert(
    goal_key: str,
    text: str,
    *,
    confirmed: bool | None = None,
    scorer_id: int | None = None,
    scorer_kind: str | None = None,
    cancelled: bool | None = None,
) -> None:
    """Update a previously sent goal post without changing its Telegram ID."""
    fields = ["text = ?", "updated_at = datetime('now')"]
    params: list[Any] = [text]
    if confirmed is not None:
        fields.append("confirmed = ?")
        params.append(int(bool(confirmed)))
    if scorer_id is not None:
        fields.append("scorer_id = ?")
        params.append(scorer_id)
    if scorer_kind is not None:
        fields.append("scorer_kind = ?")
        params.append(scorer_kind)
    if cancelled is not None:
        fields.append("cancelled = ?")
        params.append(int(bool(cancelled)))
    params.append(goal_key)
    with _connect() as conn:
        conn.execute(
            f"UPDATE goal_alerts SET {', '.join(fields)} WHERE goal_key = ?",
            tuple(params),
        )


_MANUAL_BACKUP = DB_PATH.parent / "manual_data.json"


def backup_manual_data() -> None:
    """Export manually-entered columns before a DB rebuild, so they survive a wipe."""
    players = query(
        "SELECT id, alias, first_name_fa, second_name_fa, web_name_fa FROM players "
        "WHERE alias IS NOT NULL OR first_name_fa IS NOT NULL "
        "   OR second_name_fa IS NOT NULL OR web_name_fa IS NOT NULL"
    )
    teams = query(
        "SELECT id, name_fa, short_name_fa FROM teams "
        "WHERE name_fa IS NOT NULL OR short_name_fa IS NOT NULL"
    )
    data = {"players": players, "teams": teams}
    with open(_MANUAL_BACKUP, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(
        "Backed up %d players and %d teams manual data to %s",
        len(players), len(teams), _MANUAL_BACKUP,
    )


def restore_manual_data() -> None:
    """Re-apply previously backed-up manual data after a DB rebuild."""
    if not _MANUAL_BACKUP.exists():
        logger.info("No manual data backup found, skipping restore")
        return

    try:
        with open(_MANUAL_BACKUP, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read manual data backup: %s", e)
        return

    restored_players = 0
    with _connect() as conn:
        for p in data.get("players", []):
            conn.execute(
                """UPDATE players SET alias=?, first_name_fa=?, second_name_fa=?, web_name_fa=?
                   WHERE id=?""",
                (p.get("alias"), p.get("first_name_fa"), p.get("second_name_fa"),
                 p.get("web_name_fa"), p["id"]),
            )
            restored_players += 1

        for t in data.get("teams", []):
            conn.execute(
                "UPDATE teams SET name_fa=?, short_name_fa=? WHERE id=?",
                (t.get("name_fa"), t.get("short_name_fa"), t["id"]),
            )

    logger.info(
        "Restored manual data for %d players and %d teams",
        restored_players, len(data.get("teams", [])),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    backup_manual_data()
    init_db()
    import_bootstrap("/tmp/fpl_bootstrap.json")
    import_fixtures("/tmp/fpl_fixtures.json")
    restore_manual_data()
    logger.info("Import complete. DB at %s", get_db_path())
