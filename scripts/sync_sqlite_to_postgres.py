"""Copy serving tables from local SQLite onto Render Postgres.

Skips heavy training-only tables (plays, depth charts, participation, etc.)
so the free Postgres disk stays under ~1GB while the website boards work.

Usage (from repo root, with CLOUD_DATABASE_URL set to the external Postgres URL):

  .\\.venv\\Scripts\\python.exe scripts\\sync_sqlite_to_postgres.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "packages" / "core"),
    str(ROOT / "services" / "api"),
    str(ROOT / "services" / "worker"),
]

# Parent → child order for inserts. Truncate uses CASCADE the other way.
SERVING_TABLES: list[str] = [
    "sports",
    "leagues",
    "seasons",
    "teams",
    "venues",
    "players",
    "provider_id_map",
    "markets",
    "bookmakers",
    "games",
    "rosters",
    "roster_weeks",
    "outcomes",
    "closing_lines",
    "odds_ticks",
    "weather_snapshots",
    "injury_events",
    "model_versions",
    "forecasts",
    "recommendations",
    "settlements",
    "model_health_snapshots",
    "shadow_predictions",
    "shadow_settlements",
    "ats_predictions",
    "ats_settlements",
    # Research extras used by props / parlays / desk depth
    "player_game_stats",
    "team_game_stats",
    "features",
]

BATCH = 1000


def _normalize_pg(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _sqlite_url() -> str:
    raw = os.environ.get("SOURCE_DATABASE_URL") or os.environ.get("DATABASE_URL") or "sqlite:///./data/caliber.db"
    if raw.startswith("sqlite:///./"):
        return "sqlite:///" + str((ROOT / raw[len("sqlite:///./") :]).resolve()).replace("\\", "/")
    if raw.startswith("sqlite:///"):
        return raw
    # Prefer explicit local sqlite for this tool
    return "sqlite:///" + str((ROOT / "data" / "caliber.db").resolve()).replace("\\", "/")


def _coerce(val, typ_name: str, max_len: int | None = None):
    if val is None:
        return None
    t = (typ_name or "").lower()
    if "bool" in t:
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(int(val))
        if isinstance(val, str):
            return val.strip().lower() in {"1", "true", "t", "yes", "y"}
        return bool(val)
    if "json" in t:
        from psycopg.types.json import Json

        # SQLite may already give list/dict; wrap for psycopg.
        if isinstance(val, (dict, list)):
            return Json(val)
        if isinstance(val, str):
            import json

            try:
                return Json(json.loads(val))
            except Exception:
                return Json(val)
        return Json(val)
    if isinstance(val, str) and max_len and max_len > 0 and len(val) > max_len:
        return val[:max_len]
    return val


def _row_to_dict(
    cols: list[str],
    row,
    typemap: dict[str, str],
    lenmap: dict[str, int | None],
) -> dict:
    out = {}
    for i, col in enumerate(cols):
        out[col] = _coerce(row[i], typemap.get(col, ""), lenmap.get(col))
    return out


def sync_table(src: Engine, dest: Engine, table: str) -> int:
    src_insp = inspect(src)
    dest_insp = inspect(dest)
    if table not in src_insp.get_table_names():
        print(f"skip {table}: missing in sqlite", flush=True)
        return 0
    if table not in dest_insp.get_table_names():
        print(f"skip {table}: missing in postgres", flush=True)
        return 0

    src_cols = [c["name"] for c in src_insp.get_columns(table)]
    dest_col_meta = {c["name"]: c for c in dest_insp.get_columns(table)}
    cols = [c for c in src_cols if c in dest_col_meta]
    if not cols:
        print(f"skip {table}: no overlapping columns", flush=True)
        return 0
    typemap = {c: str(dest_col_meta[c]["type"]) for c in cols}
    lenmap: dict[str, int | None] = {}
    for c in cols:
        typ = dest_col_meta[c]["type"]
        length = getattr(typ, "length", None)
        lenmap[c] = int(length) if length else None

    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    insert_sql = text(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})')

    # Free Postgres cannot hold every historical roster_week row. Keep the current
    # season (plus prior year for late-season lookup) so NFL props can resolve.
    where_sql = ""
    if table == "roster_weeks":
        where_sql = " WHERE season_year >= 2025"

    t0 = time.time()
    total = 0
    with src.connect() as sconn, dest.begin() as dconn:
        # Replace just this table so --continue can resume after a failure.
        dconn.execute(text(f'TRUNCATE "{table}" CASCADE'))
        result = sconn.execute(text(f'SELECT {col_list} FROM "{table}"{where_sql}'))
        batch: list[dict] = []
        while True:
            rows = result.fetchmany(BATCH)
            if not rows:
                break
            for row in rows:
                batch.append(_row_to_dict(cols, row, typemap, lenmap))
            if len(batch) >= BATCH:
                dconn.execute(insert_sql, batch)
                total += len(batch)
                batch.clear()
                if total % 10000 == 0:
                    print(f"  {table}: {total}…", flush=True)
        if batch:
            dconn.execute(insert_sql, batch)
            total += len(batch)
    print(f"ok {table}: {total} rows in {time.time() - t0:.1f}s", flush=True)
    return total


def main() -> int:
    cloud = os.environ.get("CLOUD_DATABASE_URL") or os.environ.get("TARGET_DATABASE_URL")
    if not cloud:
        print("Set CLOUD_DATABASE_URL to the Render external Postgres URL", file=sys.stderr)
        return 2

    continue_from = os.environ.get("SYNC_CONTINUE_FROM", "").strip()
    only = {t.strip() for t in os.environ.get("SYNC_ONLY", "").split(",") if t.strip()}

    # Point settings at cloud before init_db, but always read rows from local sqlite.
    os.environ["DATABASE_URL"] = cloud
    import spe.db.session as sess  # noqa: WPS433

    sess._engine = None
    sess._SessionLocal = None
    from spe.db.session import init_db  # noqa: WPS433

    print("ensure cloud schema…", flush=True)
    init_db()

    src = create_engine(_sqlite_url(), future=True)
    dest = create_engine(_normalize_pg(cloud), future=True, pool_pre_ping=True)

    present = [t for t in SERVING_TABLES if t in inspect(src).get_table_names()]
    if only:
        present = [t for t in present if t in only]
    if continue_from:
        if continue_from not in present:
            if only:
                print(
                    f"SYNC_CONTINUE_FROM={continue_from} not in SYNC_ONLY={sorted(only)}; "
                    "ignoring continue cursor",
                    flush=True,
                )
            else:
                # allow continuing even if earlier tables were already loaded
                present = [t for t in SERVING_TABLES if t in inspect(src).get_table_names()]
        if continue_from in present:
            present = present[present.index(continue_from) :]

    print(f"source={src.url.render_as_string(hide_password=True)}", flush=True)
    print(f"dest={dest.url.render_as_string(hide_password=True)}", flush=True)
    print(f"tables={len(present)} start={present[0] if present else None}", flush=True)

    dest_tables = set(inspect(dest).get_table_names())
    wipe = [t for t in reversed(present) if t in dest_tables]
    # Full wipe only on fresh runs (no continue/only). Per-table truncate happens in sync_table.
    if wipe and not continue_from and not only:
        joined = ", ".join(f'"{t}"' for t in wipe)
        print(f"truncate {len(wipe)} tables…", flush=True)
        with dest.begin() as conn:
            conn.execute(text(f"TRUNCATE {joined} CASCADE"))

    grand = 0
    for table in present:
        grand += sync_table(src, dest, table)
        with dest.connect() as conn:
            size_bytes = conn.execute(text("select pg_database_size(current_database())")).scalar() or 0
        if size_bytes > 900 * 1024 * 1024:
            print(f"STOP near free-disk limit after {table}: {size_bytes} bytes", flush=True)
            break

    with dest.connect() as conn:
        size = conn.execute(text("select pg_size_pretty(pg_database_size(current_database()))")).scalar()
        games = conn.execute(text("select count(*) from games")).scalar()
        players = conn.execute(text("select count(*) from players")).scalar()
        forecasts = conn.execute(text("select count(*) from forecasts")).scalar()
        ticks = conn.execute(text("select count(*) from odds_ticks")).scalar()
        shadows = conn.execute(text("select count(*) from shadow_predictions")).scalar()
    print(
        f"DONE rows={grand} size={size} games={games} players={players} "
        f"forecasts={forecasts} odds_ticks={ticks} shadow_predictions={shadows}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
