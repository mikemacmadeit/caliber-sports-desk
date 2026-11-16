"""Refresh free NFL research inputs independently of odds polling.

Only completed games are stored. A failed/empty feed retains the previous good rows.
Publication times are not supplied by these files: the 48-hour lag is an explicit
retrospective availability assumption, not proof of an archived pregame snapshot.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from spe.config import settings

from spe.analytics.nflverse_opportunities import PBP_CONTEXT_COLUMNS, pbp_context, snap_context
from spe.db.base import as_utc
from spe.db.models import Game, IngestRun, NflverseGameContext, Player, PlayerGameStat
from spe.ingest.nflverse_historical import load_asset, ingest_nfl_history
from spe.ingest.nflverse_extra import ingest_depth_frame, ingest_ngs_frame, ingest_roster_frame, NGS_FILES
from spe.ingest.store import start_run, finish_run, upsert_coverage

CONTEXT_LAG = timedelta(hours=48)


def store_context(session, records, *, year, observed_at=None):
    observed = as_utc(observed_at) or datetime.now(UTC)
    games = {g.game_id: g for g in session.query(Game).filter(
        Game.league_id == "nfl", Game.season_id == f"nfl-{year}", Game.status == "final")}
    known = {p for (p,) in session.query(Player.player_id)}
    existing = {(r.game_id, r.entity_id, r.dataset): r for r in session.query(NflverseGameContext)
                .join(Game, Game.game_id == NflverseGameContext.game_id).filter(Game.season_id == f"nfl-{year}")}
    stats = {(r.game_id, r.player_id): r for r in session.query(PlayerGameStat).join(Game)
             .filter(Game.season_id == f"nfl-{year}", PlayerGameStat.provider == "nflverse")}
    count = 0
    accepted = set()
    for rec in records:
        g = games.get(rec["game_id"])
        if g is None or as_utc(g.kickoff_utc) + CONTEXT_LAG > observed:
            continue
        if rec["team_id"] not in (g.home_team_id, g.away_team_id):
            continue
        if rec["dataset"] != "pbp_team" and rec["entity_id"] not in known:
            continue
        key = (rec["game_id"], rec["entity_id"], rec["dataset"])
        accepted.add(key)
        item = existing.get(key)
        if item is None:
            item = NflverseGameContext(**rec, provider="nflverse",
                provider_record_id=":".join(key), observed_at=observed,
                effective_at=as_utc(g.kickoff_utc) + CONTEXT_LAG,
                quality_status="postgame_lag_assumed")
            session.add(item)
            existing[key] = item
        else:
            if item.metrics != rec["metrics"]:
                item.revision += 1
                item.metrics = rec["metrics"]
                item.observed_at = observed
            # An unchanged fetch does not make already-known history newly
            # observed. Revisions receive a fresh timestamp; ingestion still logs
            # every fetch independently.
            item.ingested_at = observed
        count += 1
    # Each nonempty source is a complete game snapshot. Remove vanished rows only
    # for games/datasets represented in this refresh, never for empty/failed files.
    refreshed = {(gid, dataset) for gid, _, dataset in accepted}
    refreshed |= {(gid, "pbp_player") for gid, _, dataset in accepted if dataset == "pbp_team"}
    for key, item in list(existing.items()):
        if (key[0], key[2]) in refreshed and key not in accepted:
            session.delete(item)
            del existing[key]
    # Refresh all NFL box-score opportunity values from the complete game, including
    # players with zero scoring-area touches. Corrections may decrease a count.
    for (gid, pid), stat in stats.items():
        team = existing.get((gid, stat.team_id, "pbp_team"))
        if team is None or team.metrics.get("field_position_coverage", 0) < .98:
            continue
        player = existing.get((gid, pid, "pbp_player"))
        m = player.metrics if player else {}
        stat.gl_carries = int(m.get("inside5_carries", 0))
        stat.rz_carries = int(m.get("inside20_carries", 0))
        stat.rz_targets = int(m.get("inside20_targets", 0))
        stat.quality_status = "pbp_opportunities"
    session.flush()
    return count


def refresh_nflverse_desk(session, *, year=None, now=None, force=False):
    now = as_utc(now) or datetime.now(UTC)
    year = year or now.year - int(now.month < 7)
    from spe.ingest.nfl_availability import refresh_nfl_availability
    availability = (refresh_nfl_availability(session,now=now) if year == now.year-int(now.month<7)
                    and settings.app_env != 'test' else {'status':'not_current_season'})
    recent = session.query(IngestRun).filter(IngestRun.provider == "nflverse", IngestRun.season_year == year,
        IngestRun.dataset == "desk_context", IngestRun.status.in_(["ok", "degraded"]),
        IngestRun.started_at > now - timedelta(hours=6)).order_by(IngestRun.started_at.desc()).first()
    if recent and not force:
        retry = as_utc(recent.started_at) + timedelta(hours=6 if recent.status == "ok" else 1)
        if now < retry:
            return {"status": "not_due" if recent.status == "ok" else "degraded", "next_refresh_at": retry.isoformat(), 'availability':availability}
    run = start_run(session, "nflverse", "desk_context", "nfl", year)
    run.started_at = now
    results = {'availability':availability}

    def attempt(name, operation):
        try:
            with session.begin_nested():
                result = operation()
                if isinstance(result, dict) and result.get("rows") == 0:
                    raise ValueError("No usable rows; retain previous source data")
                results[name] = result
        except Exception as exc:
            results[name] = {"status": "unavailable", "error": type(exc).__name__}
            upsert_coverage(session, provider="nflverse", dataset=name, league_id="nfl",
                season_year=year, row_count=0, status="unavailable",
                notes=f"Refresh failed ({type(exc).__name__}); previous good rows retained.")

    attempt("box_scores", lambda: ingest_nfl_history(session, seasons=[year], refresh=True))

    def pbp():
        frame = load_asset("pbp", year, refresh=True, columns=PBP_CONTEXT_COLUMNS)
        positions = {p.player_id.removeprefix("nfl:"): p.position_group for p in session.query(Player).filter(Player.player_id.like("nfl:%"))}
        records = pbp_context(frame, positions)
        if not records:
            raise ValueError("Empty PBP source")
        count = store_context(session, records, year=year, observed_at=now)
        upsert_coverage(session, provider="nflverse", dataset="desk_pbp", league_id="nfl",
            season_year=year, row_count=count, status="available" if count else "empty",
            notes="Inside 5/10/20, target/carry shares, end-zone estimates, distinct red-zone drives, EPA. Completed games; 48-hour assumed lag.")
        return {"rows": count}

    def snaps():
        records, unresolved = snap_context(load_asset("snap_counts", year, refresh=True), load_asset("players"))
        if not records:
            raise ValueError("Empty or unresolved snap source")
        count = store_context(session, records, year=year, observed_at=now)
        upsert_coverage(session, provider="nflverse", dataset="snap_counts", league_id="nfl",
            season_year=year, row_count=count, status="available" if count else "empty",
            notes=f"PFR via nflverse; exact PFR/GSIS mapping; unresolved={unresolved}. Snap share is not route participation.")
        return {"rows": count, "unresolved": unresolved}

    attempt("desk_pbp", pbp)
    attempt("snap_counts", snaps)
    for dataset, ingest in (("roster_weekly", ingest_roster_frame), ("depth_charts", ingest_depth_frame)):
        def refresh_list(dataset=dataset, ingest=ingest):
            frame = load_asset(dataset, year, refresh=True)
            if dataset == "depth_charts" and "dt" in frame.columns:
                import pandas as pd
                frame = frame.loc[pd.to_datetime(frame["dt"], utc=True) >= now - timedelta(days=7)]
            if frame.empty:
                raise ValueError("Empty list source")
            return ingest(session, frame, year=year, observed_at=now)
        attempt(dataset, refresh_list)
    for stat, asset in NGS_FILES.items():
        def ngs(stat=stat, asset=asset):
            frame = load_asset(asset, refresh=True)
            if frame.empty:
                raise ValueError("Empty NGS source")
            return ingest_ngs_frame(session, {stat: frame}, seasons=[year], observed_at=now)
        attempt(f"ngs_{stat}", ngs)
    bad = any(isinstance(r, dict) and r.get("status") in {"unavailable", "error"} for r in results.values())
    finish_run(session, run, status="degraded" if bad else "ok", notes="Free NFL Desk inputs. FTN participation/charting excluded.")
    return {"status": "degraded" if bad else "ok", "season": year, "datasets": results}
