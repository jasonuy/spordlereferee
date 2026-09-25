"""SQLite schema and helpers for PCAHA standings / player stats."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("PCAHA_DB", str(ROOT / "data" / "pcaha.db")))
SCORESHEET_CACHE = Path(
    os.environ.get("PCAHA_SCORESHEET_CACHE", str(ROOT / "data" / "scoresheets"))
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY,
  name TEXT,
  type TEXT,
  division TEXT,
  gender TEXT,
  category TEXT,
  season_id TEXT,
  office_id INTEGER
);

CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY,
  name TEXT,
  type TEXT,
  office_id INTEGER
);

CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY,
  name TEXT,
  short_name TEXT,
  office_id INTEGER,
  logo_url TEXT
);

CREATE TABLE IF NOT EXISTS players (
  participant_id INTEGER PRIMARY KEY,
  full_name TEXT NOT NULL,
  first_name TEXT,
  last_name TEXT
);

CREATE TABLE IF NOT EXISTS games (
  id INTEGER PRIMARY KEY,
  number TEXT,
  date TEXT,
  season_id TEXT,
  schedule_id INTEGER,
  group_id INTEGER,
  division TEXT,
  gender TEXT,
  home_team_id INTEGER,
  away_team_id INTEGER,
  home_score INTEGER,
  away_score INTEGER,
  is_approved INTEGER NOT NULL DEFAULT 0,
  scoresheet_status TEXT,
  updated_at TEXT,
  ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS team_game_stats (
  game_id INTEGER NOT NULL,
  team_id INTEGER NOT NULL,
  points REAL,
  goal_for INTEGER,
  goal_against INTEGER,
  pim REAL,
  sportsmanship REAL,
  game_result TEXT,
  win_loss_type TEXT,
  PRIMARY KEY (game_id, team_id)
);

CREATE TABLE IF NOT EXISTS lineup_entries (
  game_id INTEGER NOT NULL,
  team_id INTEGER NOT NULL,
  participant_id INTEGER NOT NULL,
  number INTEGER,
  positions TEXT,
  is_affiliate INTEGER NOT NULL DEFAULT 0,
  is_starter INTEGER NOT NULL DEFAULT 0,
  dressed INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (game_id, team_id, participant_id)
);

CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  game_id INTEGER NOT NULL,
  team_id INTEGER NOT NULL,
  participant_id INTEGER,
  period TEXT,
  minutes INTEGER,
  seconds INTEGER,
  is_powerplay INTEGER NOT NULL DEFAULT 0,
  is_shorthanded INTEGER NOT NULL DEFAULT 0,
  is_empty_net INTEGER NOT NULL DEFAULT 0,
  is_penalty_shot INTEGER NOT NULL DEFAULT 0,
  is_game_winner INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assists (
  goal_id TEXT NOT NULL,
  participant_id INTEGER NOT NULL,
  ordinal INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (goal_id, participant_id)
);

CREATE TABLE IF NOT EXISTS penalties (
  id TEXT PRIMARY KEY,
  game_id INTEGER NOT NULL,
  team_id INTEGER NOT NULL,
  participant_id INTEGER,
  duration TEXT,
  pim REAL NOT NULL DEFAULT 0,
  infraction TEXT,
  period TEXT,
  minutes INTEGER,
  seconds INTEGER
);

CREATE TABLE IF NOT EXISTS goalie_stints (
  game_id INTEGER NOT NULL,
  team_id INTEGER NOT NULL,
  participant_id INTEGER NOT NULL,
  is_starter INTEGER NOT NULL DEFAULT 0,
  period TEXT,
  minutes INTEGER,
  seconds INTEGER,
  PRIMARY KEY (game_id, team_id, participant_id, period, minutes, seconds)
);

CREATE TABLE IF NOT EXISTS standings (
  season_id TEXT NOT NULL,
  schedule_id INTEGER NOT NULL,
  group_id INTEGER NOT NULL DEFAULT 0,
  team_id INTEGER NOT NULL,
  team_name TEXT,
  gp INTEGER NOT NULL DEFAULT 0,
  w INTEGER NOT NULL DEFAULT 0,
  l INTEGER NOT NULL DEFAULT 0,
  t INTEGER NOT NULL DEFAULT 0,
  otl INTEGER NOT NULL DEFAULT 0,
  pts REAL NOT NULL DEFAULT 0,
  gf INTEGER NOT NULL DEFAULT 0,
  ga INTEGER NOT NULL DEFAULT 0,
  gd INTEGER NOT NULL DEFAULT 0,
  pim REAL NOT NULL DEFAULT 0,
  sportsmanship REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (season_id, schedule_id, group_id, team_id)
);

CREATE TABLE IF NOT EXISTS player_stats (
  season_id TEXT NOT NULL,
  schedule_id INTEGER NOT NULL,
  team_id INTEGER NOT NULL,
  participant_id INTEGER NOT NULL,
  player_name TEXT,
  number INTEGER,
  positions TEXT,
  is_affiliate INTEGER NOT NULL DEFAULT 0,
  is_goalie INTEGER NOT NULL DEFAULT 0,
  gp INTEGER NOT NULL DEFAULT 0,
  g INTEGER NOT NULL DEFAULT 0,
  a INTEGER NOT NULL DEFAULT 0,
  p INTEGER NOT NULL DEFAULT 0,
  pim REAL NOT NULL DEFAULT 0,
  ppg INTEGER NOT NULL DEFAULT 0,
  shg INTEGER NOT NULL DEFAULT 0,
  gwg INTEGER NOT NULL DEFAULT 0,
  affiliate_gp INTEGER NOT NULL DEFAULT 0,
  goalie_gp INTEGER NOT NULL DEFAULT 0,
  goalie_w INTEGER NOT NULL DEFAULT 0,
  goalie_l INTEGER NOT NULL DEFAULT 0,
  goalie_t INTEGER NOT NULL DEFAULT 0,
  ga INTEGER NOT NULL DEFAULT 0,
  gaa REAL,
  PRIMARY KEY (season_id, schedule_id, team_id, participant_id)
);

CREATE INDEX IF NOT EXISTS idx_games_season_schedule ON games(season_id, schedule_id, group_id);
CREATE INDEX IF NOT EXISTS idx_games_date ON games(date);
CREATE INDEX IF NOT EXISTS idx_lineup_player ON lineup_entries(participant_id);
CREATE INDEX IF NOT EXISTS idx_goals_player ON goals(participant_id);
CREATE INDEX IF NOT EXISTS idx_player_stats_name ON player_stats(player_name);
CREATE INDEX IF NOT EXISTS idx_standings_lookup ON standings(season_id, schedule_id, group_id);
CREATE INDEX IF NOT EXISTS idx_teams_name ON teams(name);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    SCORESHEET_CACHE.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(teams)").fetchall()}
    if "logo_url" not in cols:
        conn.execute("ALTER TABLE teams ADD COLUMN logo_url TEXT")
        conn.commit()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
