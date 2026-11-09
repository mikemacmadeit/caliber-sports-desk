from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from spe.db.base import utcnow
from spe.db.models import DataCoverage, IngestRun, League, ProviderIdMap, Season, Sport, Team, Venue
from spe.ingest.canonical import NFL_TEAM_NAMES, nfl_team_id


def ensure_football_leagues(session: Session) -> None:
    if session.get(Sport, "football") is None:
        session.add(Sport(sport_id="football", name="American football"))
        session.flush()
    if session.get(League, "nfl") is None:
        session.add(
            League(
                league_id="nfl",
                sport_id="football",
                name="National Football League",
                classification="nfl",
            )
        )
    if session.get(League, "fbs") is None:
        session.add(
            League(
                league_id="fbs",
                sport_id="football",
                name="NCAA Division I FBS",
                classification="fbs",
            )
        )
    session.flush()


def ensure_season(session: Session, league_id: str, year: int) -> Season:
    sid = f"{league_id}-{int(year)}"
    row = session.get(Season, sid)
    if row:
        return row
    start = datetime(year, 8, 1, tzinfo=UTC) if league_id == "fbs" else datetime(year, 9, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 20, tzinfo=UTC) if league_id == "fbs" else datetime(year + 1, 2, 15, tzinfo=UTC)
    row = Season(season_id=sid, league_id=league_id, year=int(year), start_at=start, end_at=end)
    session.add(row)
    session.flush()
    return row


def ensure_nfl_team(session: Session, abbr: str) -> Team:
    abbr = str(abbr).strip().upper()
    tid = nfl_team_id(abbr)
    row = session.get(Team, tid)
    if row:
        return row
    row = Team(
        team_id=tid,
        league_id="nfl",
        name=NFL_TEAM_NAMES.get(abbr, abbr),
        abbreviation=abbr[:16],
        conference=None,
        aliases=[],
    )
    session.add(row)
    map_provider(session, "nflverse", "team", abbr, tid)
    session.flush()
    return row


def ensure_venue(
    session: Session,
    venue_id: str,
    name: str,
    *,
    roof: str = "open",
    surface: str = "unknown",
    lat: float = 0.0,
    lon: float = 0.0,
) -> Venue:
    row = session.get(Venue, venue_id)
    if row:
        return row
    roof_norm = (roof or "open").lower()
    if roof_norm in {"dome", "closed"}:
        roof_type = "dome"
    elif "retract" in roof_norm:
        roof_type = "retractable"
    else:
        roof_type = "open"
    row = Venue(
        venue_id=venue_id,
        name=name[:128] or venue_id,
        lat=lat,
        lon=lon,
        altitude_m=0,
        roof_type=roof_type,
        surface=(surface or "unknown")[:32],
    )
    session.add(row)
    session.flush()
    return row


def map_provider(
    session: Session,
    provider: str,
    entity_type: str,
    external_id: str,
    canonical_id: str,
    confidence: float = 1.0,
) -> None:
    existing = (
        session.query(ProviderIdMap)
        .filter_by(provider=provider, entity_type=entity_type, external_id=str(external_id))
        .one_or_none()
    )
    if existing:
        return
    session.add(
        ProviderIdMap(
            provider=provider,
            entity_type=entity_type,
            external_id=str(external_id),
            canonical_id=canonical_id,
            confidence=confidence,
            review_status="auto",
        )
    )


def start_run(session: Session, provider: str, dataset: str, league_id: str, season_year: int | None) -> IngestRun:
    run = IngestRun(
        provider=provider,
        dataset=dataset,
        league_id=league_id,
        season_year=season_year,
        status="running",
    )
    session.add(run)
    session.flush()
    return run


def finish_run(
    session: Session,
    run: IngestRun,
    *,
    status: str,
    upserted: int = 0,
    skipped: int = 0,
    notes: str = "",
    error: str | None = None,
) -> None:
    run.status = status
    run.rows_upserted = upserted
    run.rows_skipped = skipped
    run.finished_at = utcnow()
    run.notes = notes
    run.error = error


def upsert_coverage(
    session: Session,
    *,
    provider: str,
    dataset: str,
    league_id: str,
    season_year: int,
    row_count: int,
    status: str,
    notes: str = "",
) -> DataCoverage:
    row = (
        session.query(DataCoverage)
        .filter_by(provider=provider, dataset=dataset, league_id=league_id, season_year=season_year)
        .one_or_none()
    )
    if row is None:
        row = DataCoverage(
            provider=provider,
            dataset=dataset,
            league_id=league_id,
            season_year=season_year,
            row_count=row_count,
            status=status,
            notes=notes,
        )
        session.add(row)
    else:
        row.row_count = row_count
        row.status = status
        row.notes = notes
    row.last_ingested_at = utcnow()
    return row


def nfl_season_from_game(season_id: str) -> int:
    if season_id.startswith("nfl-"):
        return int(season_id.split("-", 1)[1])
    if season_id.startswith("fbs-"):
        return int(season_id.split("-", 1)[1])
    raise ValueError(season_id)
