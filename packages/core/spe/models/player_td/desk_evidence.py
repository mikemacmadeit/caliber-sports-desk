"""Cutoff-aware measured matchup evidence shared by training, live and replay.

Historical exports are revised data, not pregame snapshots. Replay explicitly
opts into a 48-hour postgame availability assumption; live requires observation.
No route, injury, weather or coverage observation is manufactured from a box score.
"""
import json
from collections import defaultdict
from datetime import timedelta
from functools import lru_cache

from spe.config import settings
from spe.db.base import as_utc

METRICS = ["pass_epa", "rush_epa", "pass_success", "rush_success", "pass_yards",
           "rush_yards", "explosive_rate", "neutral_pass_rate", "plays", "red_zone_drives",
           "red_zone_td_rate", "pace", "pass_schedule_residual", "rush_schedule_residual"]
MATCHUP_FEATURES = [f"{side}_{key}" for side in ("offense", "defense") for key in METRICS]
MATCHUP_FEATURES += ["offense_evidence_known", "defense_evidence_known", "pass_matchup", "rush_matchup",
                     "recent_rush_yards", "recent_rec_yards", "yardage_known"]
PLAYER_METRICS = ["targets", "inside20_carries", "inside5_carries", "inside20_targets", "inside5_targets"]
PLAYER_FEATURES = ["measured_" + k for k in PLAYER_METRICS] + ["player_pbp_known"]
RICH_FEATURES = MATCHUP_FEATURES + PLAYER_FEATURES
FREE_FEATURES = ["free_carries", "free_targets", "free_inside20_carries", "free_inside5_carries",
                 "free_inside20_targets", "free_inside5_targets", "rush_pbp_known", "target_pbp_known",
                 "rush_pbp_games", "target_pbp_games", "free_carry_trend", "free_target_trend"]
TEAM_RICH_FEATURES = MATCHUP_FEATURES[:len(METRICS)*2] + ["offense_evidence_known", "defense_evidence_known", "pass_matchup", "rush_matchup"]


def weighted(rows, key):
    pairs = [(0.8**i, r.get(key)) for i, r in enumerate(reversed(rows)) if r.get(key) is not None]
    return sum(w*v for w, v in pairs)/sum(w for w, _ in pairs) if pairs else None


class EvidenceHistory:
    def __init__(self, payload=None, *, replay=False):
        self.replay = replay
        self.teams, self.defenses, self.players = defaultdict(list), defaultdict(list), defaultdict(list)
        self.game_teams = {}
        self.situations, self.opponent_situations = defaultdict(list), defaultdict(list)
        for row in (payload or {}).get("situations", []):
            self.situations[row["team_id"]].append(row)
            self.opponent_situations[row["opponent_id"]].append(row)
        for row in (payload or {}).get("teams", []):
            self.teams[row["team_id"]].append(row)
            self.defenses[row["opponent_id"]].append(row)
            self.game_teams[(row["game_id"].split(":")[-1],row["team_id"])] = row
        for row in (payload or {}).get("opportunity_games", []):
            self.game_teams[(row["game_id"].split(":")[-1],row["team_id"])] = row
        for row in (payload or {}).get("players", []):
            self.players[(row["player_id"], row["team_id"])].append(row)
        for collection in (self.teams, self.defenses, self.players, self.situations, self.opponent_situations):
            for rows in collection.values():
                rows.sort(key=lambda r: r["kickoff_utc"])

    def recent(self, rows, cutoff):
        start = cutoff.replace(month=7, day=1, hour=0, minute=0, second=0, microsecond=0)
        if cutoff.month < 7:
            start = start.replace(year=start.year-1)
        return [r for r in rows if start <= as_utc(r["kickoff_utc"]) < cutoff-timedelta(hours=48)
                and (self.replay or as_utc(r["observed_at"]) <= cutoff)][-6:]

    def features(self, team, opponent, cutoff, player=None, appearances=None):
        result = {k: None for k in RICH_FEATURES + FREE_FEATURES}
        from spe.models.player_td.situational import features
        result.update(features(self.recent(self.situations[team], cutoff), self.recent(self.opponent_situations[opponent], cutoff)))
        for side, rows in (("offense", self.teams[team]), ("defense", self.defenses[opponent])):
            recent = self.recent(rows, cutoff)
            known = len(recent) >= 2 and cutoff-as_utc(recent[-1]["kickoff_utc"]) <= timedelta(days=21)
            result[side+"_evidence_known"] = float(known)
            if known:
                result.update({side+"_"+key: weighted(recent, key.replace("schedule_residual", "allowed_residual")
                    if side == "defense" else key) for key in METRICS})
        for kind in ("pass", "rush"):
            a, b = result["offense_"+kind+"_epa"], result["defense_"+kind+"_epa"]
            # A learned interaction, not an arbitrary probability multiplier.
            result[kind+"_matchup"] = a*b if a is not None and b is not None else None
        recent = self.recent(self.players[(player, team)], cutoff) if player else []
        if appearances is not None:
            indexed = {r["game_id"].split(":")[-1]:r for r in recent}
            aligned = []
            for appearance in appearances:
                game_key = appearance["game_id"].split(":")[-1]
                game = self.game_teams.get((game_key,team))
                if not game or not self.recent([game],cutoff):
                    aligned.append({"identity_coverage":0})
                    continue
                # Zero touches is justified only by a measured team game plus a
                # box-score appearance, never by absence from a player feed alone.
                aligned.append(indexed.get(game_key, {
                    **game, **{k:0. for k in PLAYER_METRICS + ["carries"]},
                    "identity_coverage":min(game.get("identity_coverage") or 0,game.get("field_coverage") or 0),
                    "rush_coverage":min(game.get("rush_coverage") or 0,game.get("field_coverage") or 0),
                    "target_coverage":min(game.get("target_coverage") or 0,game.get("field_coverage") or 0)}))
            recent = aligned
        known = len(recent) >= 2 and all(r.get("identity_coverage", 0) >= .95 for r in recent)
        result["player_pbp_known"] = float(known)
        if known:
            result.update({"measured_"+k: weighted(recent, k) for k in PLAYER_METRICS})
        for kind,metrics in (("rush",["carries","inside20_carries","inside5_carries"]),
                             ("target",["targets","inside20_targets","inside5_targets"])):
            measured = [{k:r.get(k) if r.get(kind+"_coverage",0)>=.95 else None for k in metrics} for r in recent]
            count = sum(r[metrics[0]] is not None for r in measured)
            result[kind+"_pbp_games"] = count
            result[kind+"_pbp_known"] = float(count >= 2)
            if count >= 2:
                result.update({"free_"+k:weighted(measured,k) for k in metrics})
                latest=weighted(measured[-2:],metrics[0])
                result["free_"+("carry" if kind=="rush" else "target")+"_trend"] = latest-weighted(measured,metrics[0]) if latest is not None else None
        return result


@lru_cache(maxsize=4)
def _load(path, modified, replay):
    return EvidenceHistory(json.loads(path.read_text()), replay=replay)


def load_evidence(league, *, replay=False):
    path = settings.parquet_dir / "desk_evidence" / (league+".json")
    if not path.exists():
        return EvidenceHistory(replay=replay)
    return _load(path, path.stat().st_mtime_ns, replay)


def current_context(db, game_id, player_id, cutoff, *, reconstructed=False, team_id=None, nfl_supplement=None):
    """Current facts shown with provenance; no untrained numeric adjustments."""
    from spe.db.models import WeatherSnapshot, DepthChartSnapshot, InjuryEvent
    from sqlalchemy import select
    weather = db.scalars(select(WeatherSnapshot).where(
        WeatherSnapshot.game_id == game_id, WeatherSnapshot.issued_at <= cutoff,
        WeatherSnapshot.observed_at <= cutoff, WeatherSnapshot.ingested_at <= cutoff,
        WeatherSnapshot.issued_at >= cutoff-timedelta(hours=24),
        WeatherSnapshot.quality_status == "nws_forecast").order_by(WeatherSnapshot.issued_at.desc())).first()
    depth = db.scalars(select(DepthChartSnapshot).where(
        DepthChartSnapshot.game_id == game_id, DepthChartSnapshot.player_id == player_id,
        DepthChartSnapshot.provider != "seed", DepthChartSnapshot.snapshot_at <= cutoff,
        DepthChartSnapshot.quality_status.notin_(("invalid", "quarantined")),
        DepthChartSnapshot.observed_at <= cutoff, DepthChartSnapshot.snapshot_at >= cutoff-timedelta(days=7)
    ).order_by(DepthChartSnapshot.snapshot_at.desc())).first()
    from sqlalchemy import or_
    injuries = db.scalars(select(InjuryEvent).where(InjuryEvent.game_id == game_id,
        InjuryEvent.reported_at <= cutoff, or_(InjuryEvent.observed_at <= cutoff, InjuryEvent.provider == 'conference_public') if reconstructed else InjuryEvent.observed_at <= cutoff,
        InjuryEvent.quality_status.notin_(("invalid", "quarantined"))).order_by(InjuryEvent.reported_at)).all()
    from spe.models.player_td.availability_status import resolved_reports
    latest = resolved_reports(injuries)
    for item in latest.values():
        item['recovered_after_cutoff'] = bool(item['observed_at'] and as_utc(item['observed_at']) > cutoff)
    personnel = personnel_context(game_id, player_id, cutoff, latest)
    supplement = nfl_supplement or {}
    if nfl_supplement is None and team_id and team_id.startswith('nfl:'):
        from spe.models.player_td.nflverse_context import supplemental_context
        supplement = supplemental_context(db, [player_id], cutoff, team_id=team_id).get(player_id, {})
    depth_context = ({"rank": depth.rank, "source": depth.provider,
        "as_of": as_utc(depth.snapshot_at).isoformat(), "observed_at": as_utc(depth.observed_at).isoformat()}
        if depth else None)
    weekly_depth = supplement.get('depth')
    if weekly_depth and (not depth_context or as_utc(weekly_depth['as_of']) > as_utc(depth_context['as_of'])):
        depth_context = weekly_depth
    return {"weather": ({"wind_mps": weather.wind_mps, "precip_probability": weather.precip_probability,
        "temperature_c": weather.temperature_c, "roof": weather.roof_status,
        "issued_at": as_utc(weather.issued_at).isoformat(), "source": weather.provider,
        "probability_effect": "not_fitted"} if weather else None),
        "depth": depth_context,
        "nflverse_context": supplement,
        "availability": latest.get(player_id), "reported_players": latest, "personnel_alerts": personnel,
        "missing": ["charted_routes", "individual_coverage_assignments", "verified_coach_role_notes"],
        "note": "Missing reports are unknown. Depth can exclude a documented backup quarterback; weather and NGS do not adjust probability."}


@lru_cache(maxsize=2)
def _rosters(path, modified):
    return json.loads(path.read_text()).get("players", [])


@lru_cache(maxsize=16)
def _recent_rosters(path, modified, cutoff):
    return [r for r in _rosters(path, modified)
        if as_utc(r['observed_at']) <= cutoff and cutoff-as_utc(r['observed_at']) <= timedelta(days=7)]


def personnel_context(game_id, player_id, cutoff, reports):
    # This source contains college identities only. Do not scan it for NFL
    # players or infer NFL personnel assignments from a college roster.
    if not player_id or player_id.startswith('nfl:'):
        return []
    path=settings.parquet_dir / "desk_evidence/fbs_rosters.json"
    if not path.exists() or not reports:
        return []
    rows = _recent_rosters(path, path.stat().st_mtime_ns, cutoff)
    own={r['team_id'] for r in rows if r['player_id']==player_id}
    if len(own)!=1:
        return []
    selected=[]
    for r in rows:
        report=reports.get(r['player_id'])
        if report and report['status'] in ('out','inactive','confirmed_inactive','suspended','doubtful','questionable','uncertain'):
            selected.append({'player_id':r['player_id'],'name':r['name'],'position':r['position'],
                'side':'teammate' if r['team_id'] in own else 'opponent',**report,
                'probability_effect':'not_fitted'})
    return selected


def explanations(f):
    from spe.models.player_td.situational import explanations as situation_explanations
    reasons, risks = situation_explanations(f), []
    for side, label in (("offense", "Offense"), ("defense", "Opponent allows")):
        if f.get(side+"_evidence_known"):
            rush, passing = f.get(side+"_rush_epa"), f.get(side+"_pass_epa")
            if rush is not None and passing is not None:
                reasons.append(f"{label}: {rush:+.2f} rushing and {passing:+.2f} passing EPA per play in recent measured games.")
        else:
            risks.append(f"Recent {side} play-by-play coverage is incomplete or stale.")
    if f.get("rush_pbp_known"):
        reasons.append(f"Measured rushing role: {f['free_inside20_carries']:.1f} red-zone carries and {f['free_inside5_carries']:.1f} carries inside the five per game across {int(f['rush_pbp_games'])} measured games.")
    if f.get("target_pbp_known"):
        reasons.append(f"Measured receiving role: {f['free_targets']:.1f} targets and {f['free_inside20_targets']:.1f} red-zone targets per game across {int(f['target_pbp_games'])} measured games.")
    if f.get("player_pbp_known") and not (f.get("rush_pbp_known") or f.get("target_pbp_known")):
        reasons.append(f"Measured scoring-area work: {f['measured_inside20_carries']:.1f} red-zone carries and {f['measured_inside20_targets']:.1f} red-zone targets per game.")
    elif not (f.get("player_pbp_known") or f.get('pbp_known') or f.get('charting_known') or (f.get("rush_pbp_known") and f.get("target_pbp_known"))):
        risks.append("Player-level play-by-play identity coverage is incomplete; scoring-area opportunities remain unknown.")
    return reasons, risks
