"""Lightweight additive migrations for the local SQLite prototype."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


_COLUMNS = [
    ("games", "data_tier", "VARCHAR(16) DEFAULT 'demo' NOT NULL"),
    ("games", "home_score", "INTEGER"),
    ("games", "away_score", "INTEGER"),
    ("forecasts", "data_tier", "VARCHAR(16) DEFAULT 'demo' NOT NULL"),
    ("recommendations", "data_tier", "VARCHAR(16) DEFAULT 'demo' NOT NULL"),
    ("plays", "wp", "FLOAT"),
    ("plays", "success", "BOOLEAN"),
    ("plays", "is_pass", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("plays", "is_rush", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("plays", "is_td", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("plays", "is_turnover", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("plays", "is_red_zone", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("plays", "is_goal_line", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("plays", "is_scrimmage", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("plays", "first_down", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("plays", "game_seconds_remaining", "INTEGER"),
    ("plays", "drive_id", "VARCHAR(32)"),
    ("plays", "passer_player_id", "VARCHAR(64)"),
    ("plays", "rusher_player_id", "VARCHAR(64)"),
    ("plays", "receiver_player_id", "VARCHAR(64)"),
    ("player_game_stats", "gl_carries", "INTEGER DEFAULT 0 NOT NULL"),
    ("player_game_stats", "team_id", "VARCHAR(64)"),
    ("weather_snapshots", "precip_probability", "FLOAT"),
    ("weather_snapshots", "summary", "VARCHAR(160)"),
    ("weather_snapshots", "alerts", "JSON"),
    ("participation_plays", "possession_team", "VARCHAR(8)"),
    ("participation_plays", "offense_formation", "VARCHAR(32)"),
    ("participation_plays", "offense_personnel", "TEXT"),
    ("participation_plays", "defense_personnel", "TEXT"),
    ("participation_plays", "defenders_in_box", "FLOAT"),
    ("participation_plays", "number_of_pass_rushers", "FLOAT"),
    ("participation_plays", "route", "VARCHAR(64)"),
    ("participation_plays", "defense_man_zone_type", "VARCHAR(32)"),
    ("participation_plays", "defense_coverage_type", "VARCHAR(32)"),
    ("participation_plays", "time_to_throw", "FLOAT"),
    ("participation_plays", "was_pressure", "BOOLEAN"),
    ("participation_plays", "ngs_air_yards", "FLOAT"),
]


def ensure_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    pending: list[tuple[str, str, str]] = []
    for table, column, ddl in _COLUMNS:
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column in existing:
            continue
        pending.append((table, column, ddl))
    if not pending:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA busy_timeout=2500")
        for table, column, ddl in pending:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
