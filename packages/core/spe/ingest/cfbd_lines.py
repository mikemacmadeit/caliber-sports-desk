"""CollegeFootballData closing spreads for FBS.

CFBD `spread` is the home team's sportsbook handicap: negative means home is favored.
Stored on closing_lines.home_line with source='cfbd'. Do not negate it into nflverse L.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from spe.adapters.cfbd import CfbdAdapter
from spe.db.base import new_id
from spe.db.models import ClosingLine, Game, ProviderIdMap
from spe.ingest.store import finish_run, start_run, upsert_coverage
from spe.ingest.values import as_float, as_int, as_str

PROVIDER = "cfbd"
SOURCE = "cfbd"
DEFAULT_SEASONS = tuple(range(2014, 2027))


def consensus_home_handicap(lines: Any) -> float | None:
    """Home handicap from a CFBD lines array. Consensus provider wins; else the median."""
    if not isinstance(lines, list):
        return None
    values: list[float] = []
    consensus: float | None = None
    for row in lines:
        if not isinstance(row, dict):
            continue
        spread = as_float(row.get("spread"))
        if spread is None:
            continue
        provider = (as_str(row.get("provider")) or "").lower()
        if provider == "consensus":
            consensus = spread
        values.append(spread)
    if consensus is not None:
        return consensus
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[len(ordered) // 2])


def ingest_fbs_closing_lines(
    session: Session,
    *,
    seasons: tuple[int, ...] | list[int] = DEFAULT_SEASONS,
    adapter: CfbdAdapter | None = None,
    fixtures: dict[int, list[dict[str, Any]]] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Load one closing home handicap per FBS game. Skips games we cannot match."""
    observed = observed_at or datetime.now(UTC)
    adapter = adapter or CfbdAdapter()
    known = _game_index(session)
    existing = {
        (row.game_id, row.market_id, row.source)
        for row in session.query(ClosingLine.game_id, ClosingLine.market_id, ClosingLine.source).all()
    }
    summary: dict[str, Any] = {
        "status": "ok",
        "seasons": list(seasons),
        "lines_upserted": 0,
        "games_unmatched": 0,
        "rows_without_spread": 0,
        "sign_check": None,
    }
    probe_year = 2024 if 2024 in seasons else seasons[-1]
    probe_records = fixtures.get(probe_year) if fixtures is not None else None
    if probe_records is None:
        probe_records = list(adapter.fetch("lines", {"year": probe_year}).records)
    sign = _sign_from_records(probe_records)
    summary["sign_check"] = sign
    if sign.get("ok") is False:
        raise RuntimeError(f"CFBD spread sign check failed before write: {sign}")

    for year in seasons:
        run = start_run(session, PROVIDER, "lines", "fbs", year)
        if year == probe_year:
            records = probe_records
        elif fixtures is not None:
            records = fixtures.get(year) or []
        else:
            records = list(adapter.fetch("lines", {"year": year}).records)
        upserted = skipped = 0
        for raw in records:
            game_key = as_str(raw.get("id")) or as_str(raw.get("gameId")) or as_str(raw.get("game_id"))
            handicap = consensus_home_handicap(raw.get("lines"))
            if not game_key or handicap is None:
                summary["rows_without_spread"] += 1
                skipped += 1
                continue
            game_id = known.get(game_key)
            if game_id is None:
                summary["games_unmatched"] += 1
                skipped += 1
                continue
            key = (game_id, "point_spread", SOURCE)
            if key in existing:
                skipped += 1
                continue
            game = session.get(Game, game_id)
            captured = game.kickoff_utc if game is not None and game.kickoff_utc is not None else observed
            session.add(
                ClosingLine(
                    line_id=new_id(),
                    game_id=game_id,
                    market_id="point_spread",
                    home_line=float(handicap),
                    away_line=float(-handicap),
                    home_price_american=None,
                    away_price_american=None,
                    source=SOURCE,
                    captured_at=captured,
                    provider=PROVIDER,
                    provider_record_id=game_key[:160],
                    observed_at=observed,
                    effective_at=captured,
                    quality_status="consensus",
                )
            )
            existing.add(key)
            upserted += 1
        summary["lines_upserted"] += upserted
        finish_run(session, run, status="ok", upserted=upserted, skipped=skipped)
        upsert_coverage(
            session,
            provider=PROVIDER,
            dataset="lines",
            league_id="fbs",
            season_year=year,
            row_count=upserted,
            status="available" if upserted or records else "empty",
            notes="CFBD home handicap. Negative means the home team is favored.",
        )
        session.commit()
        print(f"SEASON_DONE fbs lines {year} upserted={upserted}", flush=True)

    return summary


def _sign_from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    favored: list[float] = []
    dogs: list[float] = []
    for raw in records:
        handicap = consensus_home_handicap(raw.get("lines"))
        home_pts = as_int(raw.get("homeScore") or raw.get("home_score"), None)
        away_pts = as_int(raw.get("awayScore") or raw.get("away_score"), None)
        if handicap is None or home_pts is None or away_pts is None:
            continue
        margin = float(home_pts - away_pts)
        if handicap <= -3:
            favored.append(margin)
        elif handicap >= 3:
            dogs.append(margin)
    return _sign_check(favored, dogs)


def _sign_check(favored: list[float], dogs: list[float]) -> dict[str, Any]:
    """Home favorites (handicap <= -3) should win by more than home dogs (handicap >= 3)."""
    out: dict[str, Any] = {"favored_n": len(favored), "dog_n": len(dogs), "ok": None}
    if len(favored) < 30 or len(dogs) < 30:
        out["ok"] = None
        out["note"] = "Too few scored games in this batch to check the sign."
        return out
    favored_mean = sum(favored) / len(favored)
    dog_mean = sum(dogs) / len(dogs)
    out["favored_mean_margin"] = favored_mean
    out["dog_mean_margin"] = dog_mean
    out["ok"] = favored_mean > 0 > dog_mean
    return out


def _game_index(session: Session) -> dict[str, str]:
    """Map a CFBD game id to the row the model trains on."""
    present = {
        str(row.game_id)
        for row in session.query(Game.game_id).filter(Game.league_id == "fbs").all()
    }
    index: dict[str, str] = {}
    for gid in present:
        if gid.startswith("espn:fbs:"):
            index.setdefault(gid.split(":")[-1], gid)
    for gid in present:
        if gid.startswith("fbs:"):
            index[gid[4:]] = gid
    for row in session.query(ProviderIdMap).filter_by(provider=PROVIDER, entity_type="game").all():
        if row.canonical_id in present:
            index[str(row.external_id)] = row.canonical_id
    return index
