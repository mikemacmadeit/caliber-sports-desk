"""Shared, cutoff-aware free NFL features for replay and the live Desk."""
from collections import defaultdict
from datetime import timedelta

from spe.db.base import as_utc
from spe.db.models import Game, NflverseGameContext, NgsWeek, DepthChartWeek
from spe.models.player_td.desk_opportunity import canonical_team

NFL_EXTRA_FEATURES = [
    "snap_share", "snap_trend", "snap_known", "inside10_carries", "inside10_targets",
    "inside5_targets", "target_share", "target_trend", "end_zone_targets_estimated",
    "end_zone_known", "team_red_zone_drives", "opponent_red_zone_drives",
    "opponent_position_targets", "opponent_position_inside10_targets",
    "opponent_position_inside5_carries", "team_neutral_pass_rate", "opponent_pass_epa",
    "opponent_rush_epa", "pbp_known",
]


def attach_context(records, contexts, aliases=None):
    aliases = aliases or {}
    index = {}
    for raw in contexts:
        r = dict(raw)
        r["team_id"] = canonical_team(r["team_id"], aliases)
        if r["dataset"] == "pbp_team":
            r["entity_id"] = r["team_id"]
        index[(r["game_id"], r["entity_id"], r["dataset"])] = r
    for r in records:
        team = canonical_team(r["team_id"], aliases)
        opponent = canonical_team(r["away_team_id"] if team == canonical_team(r["home_team_id"], aliases) else r["home_team_id"], aliases)
        # All joins use the stored game's team, never a player's current roster.
        r["nflverse_context"] = {}
        for key, entity, dataset in (("player", r["player_id"], "pbp_player"),
                ("snap", r["player_id"], "snap_counts"), ("team", team, "pbp_team"),
                ("opponent", opponent, "pbp_team")):
            context = index.get((r["game_id"], entity, dataset))
            if context and context["team_id"] == (opponent if key == "opponent" else team):
                r["nflverse_context"][key] = context
    return records


def load_context(db, records, cutoff):
    if not records:
        return records
    start = min(as_utc(r["kickoff_utc"]) for r in records)
    contexts = [{"game_id": r.game_id, "entity_id": r.entity_id, "team_id": r.team_id,
                 "dataset": r.dataset, "metrics": r.metrics, "effective_at": r.effective_at,
                 "observed_at": r.observed_at} for r in db.query(NflverseGameContext).join(Game)
                .filter(Game.kickoff_utc >= start, Game.kickoff_utc < cutoff,
                        NflverseGameContext.effective_at < cutoff,
                        NflverseGameContext.observed_at <= cutoff)]
    return attach_context(records, contexts)


def _available(row, key, cutoff):
    item = row.get("nflverse_context", {}).get(key)
    if not item:
        return None
    available = as_utc(item.get("effective_at")) or row["kickoff_utc"] + timedelta(hours=48)
    if available >= cutoff:
        return None
    observed = as_utc(item.get("observed_at"))
    if observed is not None and observed > cutoff:
        return None
    return item["metrics"]


def _mean(values):
    present = [(0.8 ** i, v) for i, v in enumerate(reversed(values)) if v is not None]
    return sum(w * v for w, v in present) / sum(w for w, v in present) if present else None


def free_features(own, offense, defense, position, cutoff):
    """Missing stays null; model vectors use explicit source-coverage indicators."""
    values = defaultdict(list)
    # Align to team games. Skipping a player's missed games falsely labels an
    # old starter's snaps and targets as current. Complete team PBP can establish
    # zero touches; missing snap rows stay unknown, never fabricated zero snaps.
    own_by_game = {r.get('game_id'):r for r in own}
    aligned = [own_by_game.get(g.get('game_id'), {
        'game_id':g.get('game_id'),'kickoff_utc':g['kickoff_utc'],
        'nflverse_context':{'team':g.get('nflverse_context',{}).get('team')}}) for g in offense]
    if aligned:
        own = aligned
    recent_touches=[]
    for r in own:
        snap = _available(r, "snap", cutoff)
        values["snap_share"].append(snap.get("offense_snap_share") if snap else None)
        team, player = _available(r, "team", cutoff), _available(r, "player", cutoff)
        known = team is not None and team.get("field_position_coverage", 0) >= .98
        # No PBP row means zero touches only when the complete team game is present.
        p = player or {}
        recent_touches.append(float(p.get('carries',0))+float(p.get('targets',0)) if known else None)
        for dest, key in (("inside10_carries", "inside10_carries"), ("inside10_targets", "inside10_targets"),
                          ("inside5_targets", "inside5_targets"), ("target_share", "target_share")):
            values[dest].append(p.get(key, 0.) if known else None)
        ez_known = known and (p.get("air_yards_coverage") or 0) >= .9
        values["end_zone_targets_estimated"].append(p.get("end_zone_targets_estimated") if ez_known else None)
    def team_values(rows, key):
        return [(m or {}).get(key) for r in rows for m in [_available(r, "team", cutoff)]]
    for dest, source, rows in (("team_red_zone_drives", "red_zone_drives", offense),
            ("opponent_red_zone_drives", "red_zone_drives", defense),
            ("team_neutral_pass_rate", "neutral_pass_rate", offense),
            ("opponent_pass_epa", "pass_epa", defense), ("opponent_rush_epa", "rush_epa", defense),
            ("opponent_position_targets", f"{position}_targets", defense),
            ("opponent_position_inside10_targets", f"{position}_inside10_targets", defense),
            ("opponent_position_inside5_carries", f"{position}_inside5_carries", defense)):
        values[dest] = team_values(rows, source)
    f = {key: _mean(v) for key, v in values.items()}
    for dest, source in (("snap_trend", "snap_share"), ("target_trend", "target_share")):
        latest = _mean(values[source][-2:])
        f[dest] = latest - f[source] if latest is not None and f[source] is not None else None
    f["snap_games"] = sum(v is not None for v in values["snap_share"])
    f["pbp_games"] = sum(v is not None for v in values["inside10_targets"])
    f["snap_known"] = float(f["snap_games"] >= 2)
    f["pbp_known"] = float(f["pbp_games"] >= 2 and f["team_red_zone_drives"] is not None and f["opponent_red_zone_drives"] is not None)
    f["end_zone_known"] = float(sum(v is not None for v in values["end_zone_targets_estimated"]) >= 2)
    f["last_snap_share"] = values["snap_share"][-1] if values["snap_share"] else None
    f["free_data_current"] = bool(f["last_snap_share"] is not None and values["inside10_targets"] and values["inside10_targets"][-1] is not None)
    f['recent_measured_opportunity_games'] = sum(v is not None for v in recent_touches[-2:])
    f['recent_measured_touches'] = sum(recent_touches[-2:])/2 if len(recent_touches)>=2 and all(v is not None for v in recent_touches[-2:]) else None
    return f


def context_explanations(f):
    reasons, risks = [], []
    if f.get("snap_games", 0) >= 2:
        trend = f.get("snap_trend")
        reasons.append(f"Playing time: {f['snap_share']:.0%} of offensive snaps across {f['snap_games']} measured recent games.")
        if trend is not None and abs(trend) >= .10:
            reasons.append(f"Last two games' snap share is {abs(trend):.0%} {'higher' if trend > 0 else 'lower'} than the recent average.")
    else:
        risks.append("Recent snap-count coverage is incomplete.")
    if f.get("snap_known") and not f.get("free_data_current"):
        risks.append("Detailed inputs for the latest appearance are not yet available.")
    if f.get("pbp_known"):
        reasons.append(f"Inside the 10: {f['inside10_carries']:.1f} carries and {f['inside10_targets']:.1f} targets per game.")
        if f.get("target_share") is not None and f["target_share"] > 0:
            reasons.append(f"Receives {f['target_share']:.0%} of the team's measured targets recently.")
        if f.get("team_red_zone_drives") is not None and f.get("opponent_red_zone_drives") is not None:
            reasons.append(f"Team reaches the red zone on {f['team_red_zone_drives']:.1f} drives per game; opponent allows {f['opponent_red_zone_drives']:.1f}.")
    else:
        risks.append("Detailed recent play-by-play coverage is incomplete.")
    if f.get("end_zone_known"):
        reasons.append(f"Estimated end-zone targets: {f['end_zone_targets_estimated']:.1f} per game, inferred from air yards and field position.")
    return reasons, risks


def supplemental_context(db, player_ids, cutoff, *, team_id=None):
    """Descriptive NGS/depth context. Not an untrained probability adjustment."""
    out = defaultdict(dict)
    if not player_ids:
        return out
    for r in db.query(NgsWeek).filter(NgsWeek.player_id.in_(player_ids), NgsWeek.week > 0,
            NgsWeek.effective_at < cutoff, NgsWeek.effective_at >= cutoff - timedelta(days=28),
            NgsWeek.observed_at <= cutoff, NgsWeek.quality_status.notin_(("season_aggregate_not_pregame", "invalid", "quarantined"))).order_by(NgsWeek.effective_at):
        if team_id and canonical_team(r.team_id, {}) != canonical_team(team_id, {}):
            continue
        out[r.player_id][r.stat_type] = {"metrics": r.metrics, "team_id": canonical_team(r.team_id, {}),
            "as_of": as_utc(r.effective_at).isoformat(), "observed_at": as_utc(r.observed_at).isoformat(),
            "source": "NFL Next Gen Stats via nflverse"}
    for r in db.query(DepthChartWeek).filter(DepthChartWeek.player_id.in_(player_ids),
            DepthChartWeek.position.in_(('QB','RB','FB','WR','TE')),
            DepthChartWeek.effective_at < cutoff, DepthChartWeek.effective_at >= cutoff - timedelta(days=7),
            DepthChartWeek.observed_at <= cutoff,
            DepthChartWeek.quality_status.notin_(("invalid", "quarantined"))).order_by(DepthChartWeek.effective_at, DepthChartWeek.depth_rank.desc()):
        if team_id and canonical_team(r.team_id, {}) != canonical_team(team_id, {}):
            continue
        if r.depth_rank is None or r.depth_rank < 1:
            continue
        out[r.player_id]["depth"] = {"rank": r.depth_rank, "position": r.position,
            "team_id": canonical_team(r.team_id, {}), "as_of": as_utc(r.effective_at).isoformat(),
            "observed_at": as_utc(r.observed_at).isoformat(), "source": "nflverse depth charts"}
    return out
