"""Backfill final scores and NFL consensus closing spreads for ATS work.

Uses nflverse games.parquet (already cached) and play-derived scores for FBS.
Does not invent lines. Does not call paid Odds historical endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.base import new_id, utcnow
from spe.db.models import ClosingLine, Game
from spe.ingest.canonical import nfl_game_id
from spe.ingest.store import upsert_coverage
from spe.ingest.values import as_float, as_int


def backfill_ats_labels(session: Session, *, refresh_nfl_parquet: bool = False) -> dict[str, Any]:
    """Populate game scores + NFL consensus spreads. Idempotent updates only fill blanks."""
    out: dict[str, Any] = {
        "nfl_scores": 0,
        "nfl_spreads": 0,
        "fbs_scores_from_plays": 0,
        "status": "ok",
    }
    out["nfl_scores"], out["nfl_spreads"] = _backfill_nfl_from_parquet(session, refresh=refresh_nfl_parquet)
    out["fbs_scores_from_plays"] = _backfill_fbs_scores_from_plays(session)
    session.flush()
    upsert_coverage(
        session,
        provider="nflverse",
        dataset="closing_spreads_consensus",
        league_id="nfl",
        season_year=0,
        row_count=out["nfl_spreads"],
        status="available" if out["nfl_spreads"] else "empty",
        notes="Consensus closing spread/total from nflverse games.parquet — not bookmaker ticks.",
    )
    upsert_coverage(
        session,
        provider="caliber",
        dataset="final_scores",
        league_id="nfl",
        season_year=0,
        row_count=out["nfl_scores"],
        status="available" if out["nfl_scores"] else "empty",
        notes="Final scores backfilled for ATS margin labels.",
    )
    upsert_coverage(
        session,
        provider="caliber",
        dataset="final_scores",
        league_id="fbs",
        season_year=0,
        row_count=out["fbs_scores_from_plays"],
        status="available" if out["fbs_scores_from_plays"] else "empty",
        notes="FBS scores reconstructed from PBP score_before columns when CFBD final scores were not stored.",
    )
    return out


def _games_parquet() -> Path:
    return Path(settings.parquet_dir).resolve().parent / "cache" / "nflverse" / "games.parquet"


def _backfill_nfl_from_parquet(session: Session, *, refresh: bool = False) -> tuple[int, int]:
    path = _games_parquet()
    if not path.exists():
        if refresh:
            from spe.ingest.nflverse_historical import load_asset

            load_asset("schedules", refresh=True)
        if not path.exists():
            return 0, 0
    df = pd.read_parquet(path)
    scores_n = spreads_n = 0
    existing_lines = {
        (r.game_id, r.market_id, r.source)
        for r in session.query(ClosingLine.game_id, ClosingLine.market_id, ClosingLine.source)
        .filter(ClosingLine.source == "nflverse_consensus")
        .all()
    }
    now = utcnow()
    for _, raw in df.iterrows():
        gid_raw = str(raw.get("game_id") or "")
        if not gid_raw:
            continue
        game_id = nfl_game_id(gid_raw)
        game = session.get(Game, game_id)
        if game is None or game.league_id != "nfl":
            continue
        hs = as_int(raw.get("home_score"), None)
        aws = as_int(raw.get("away_score"), None)
        if hs is not None and aws is not None:
            if game.home_score is None or game.away_score is None:
                game.home_score = hs
                game.away_score = aws
                if game.status != "final":
                    game.status = "final"
                scores_n += 1
        # Store raw nflverse L (expected home margin). Do NOT convert to handicap here.
        spread = as_float(raw.get("spread_line"))
        if spread is not None and (game_id, "point_spread", "nflverse_consensus") not in existing_lines:
            home_price = as_int(raw.get("home_spread_odds"), None)
            away_price = as_int(raw.get("away_spread_odds"), None)
            session.add(
                ClosingLine(
                    line_id=new_id(),
                    game_id=game_id,
                    market_id="point_spread",
                    home_line=float(spread),  # L: positive ⇒ home favored
                    away_line=float(-spread),  # expected away margin = -L
                    home_price_american=home_price,
                    away_price_american=away_price,
                    source="nflverse_consensus",
                    captured_at=as_utc_kickoff(game.kickoff_utc) or now,
                    provider="nflverse",
                    provider_record_id=gid_raw,
                    observed_at=now,
                    effective_at=as_utc_kickoff(game.kickoff_utc) or now,
                    quality_status="consensus",
                )
            )
            existing_lines.add((game_id, "point_spread", "nflverse_consensus"))
            spreads_n += 1
        total = as_float(raw.get("total_line"))
        if total is not None and (game_id, "total", "nflverse_consensus") not in existing_lines:
            session.add(
                ClosingLine(
                    line_id=new_id(),
                    game_id=game_id,
                    market_id="total",
                    home_line=float(total),
                    away_line=None,
                    home_price_american=None,
                    away_price_american=None,
                    source="nflverse_consensus",
                    captured_at=as_utc_kickoff(game.kickoff_utc) or now,
                    provider="nflverse",
                    provider_record_id=f"{gid_raw}:total",
                    observed_at=now,
                    effective_at=as_utc_kickoff(game.kickoff_utc) or now,
                    quality_status="consensus",
                )
            )
            existing_lines.add((game_id, "total", "nflverse_consensus"))
        if (scores_n + spreads_n) and (scores_n + spreads_n) % 500 == 0:
            session.flush()
    session.flush()
    return scores_n, spreads_n


def _backfill_fbs_scores_from_plays(session: Session) -> int:
    """Recover final scores from the last play scoreboard when games.home_score is null."""
    rows = session.execute(
        text(
            """
            SELECT g.game_id,
                   MAX(CASE WHEN p.offense_id = g.home_team_id THEN p.score_before_off
                            WHEN p.defense_id = g.home_team_id THEN p.score_before_def END) AS home_max,
                   MAX(CASE WHEN p.offense_id = g.away_team_id THEN p.score_before_off
                            WHEN p.defense_id = g.away_team_id THEN p.score_before_def END) AS away_max
            FROM games g
            JOIN plays p ON p.game_id = g.game_id
            WHERE g.league_id = 'fbs'
              AND g.status = 'final'
              AND g.home_score IS NULL
            GROUP BY g.game_id
            """
        )
    ).fetchall()
    n = 0
    for game_id, home_max, away_max in rows:
        if home_max is None or away_max is None:
            continue
        game = session.get(Game, game_id)
        if game is None or game.home_score is not None:
            continue
        # score_before is pre-play; final scoring plays can leave a 0–8 point undercount.
        # Prefer these reconstructed scores over nothing; quality_status lives on coverage notes.
        game.home_score = int(home_max)
        game.away_score = int(away_max)
        n += 1
        if n % 400 == 0:
            session.flush()
    session.flush()
    return n


def as_utc_kickoff(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def ensure_point_spread_market(session: Session) -> None:
    from spe.db.models import Market

    if session.get(Market, "point_spread") is None:
        session.add(
            Market(
                market_id="point_spread",
                sport="football",
                market_type="game_spread",
                period="game",
                stat="point_spread",
                grading_rule=(
                    "Sportsbook handicap h (negative = home favorite): home covers when "
                    "(home_score - away_score) + h > 0. nflverse consensus L (expected home "
                    "margin; positive = home favored) converts as h = -L; equivalently "
                    "home covers when M - L > 0. Push when equal to 0."
                ),
                odds_api_key="spreads",
            )
        )
        session.flush()
