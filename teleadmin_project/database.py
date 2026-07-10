import json
import logging
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "fpl.db"

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

CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_id);
CREATE INDEX IF NOT EXISTS idx_players_position ON players(position_id);
CREATE INDEX IF NOT EXISTS idx_players_form ON players(form);
CREATE INDEX IF NOT EXISTS idx_players_search ON players(search_name);
CREATE INDEX IF NOT EXISTS idx_fixtures_gameweek ON fixtures(gameweek_id);
CREATE INDEX IF NOT EXISTS idx_fixtures_teams ON fixtures(team_h, team_a);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    init_db()
    import_bootstrap("/tmp/fpl_bootstrap.json")
    import_fixtures("/tmp/fpl_fixtures.json")
    logger.info("Import complete. DB at %s", get_db_path())
