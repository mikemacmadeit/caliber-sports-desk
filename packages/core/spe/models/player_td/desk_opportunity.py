"""Pregame opportunity model shared by replay and the live Desk.

No prices, results from the target game, passing TDs, or depth-rank penalties.
Unknown charting stays missing. Model parameters are fit separately by league.
"""
from __future__ import annotations

import json
import hashlib
import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np

from spe.config import settings
from spe.db.base import as_utc

VERSION = "desk_opportunity_v2"
COLUMNS = ["game_id", "player_id", "team_id", "kickoff_utc", "home_team_id", "away_team_id",
           "position", "provider", "carries", "targets", "receptions", "rush_td", "rec_td",
           "gl_carries", "rz_targets", "player_name"]
FEATURES = ["carries", "receptions", "targets", "gl_carries", "rz_targets", "rush_td", "rec_td",
            "carry_trend", "receiving_trend", "team_rush_td", "team_rec_td", "opp_rush_td",
            "opp_rec_td", "opp_position_td", "carry_share", "receiving_share", "goal_line_share",
            "red_zone_share", "games", "charting_known", "is_home", "pos_qb", "pos_rb", "pos_te"]


def canonical_team(team, aliases):
    return {"nfl:LAR": "nfl:LA", "nfl:WSH": "nfl:WAS", "nfl:JAC": "nfl:JAX"}.get(
        aliases.get(team, team), aliases.get(team, team))


def prepare_history(records, aliases=None):
    aliases = aliases or {}
    rows = []
    for raw in records:
        row = dict(zip(COLUMNS, raw)) if not isinstance(raw, dict) else dict(raw)
        row["kickoff_utc"] = as_utc(row["kickoff_utc"])
        if row["kickoff_utc"] is None:
            continue
        for key in ("team_id", "home_team_id", "away_team_id"):
            row[key] = canonical_team(row[key], aliases)
        if row["team_id"] not in (row["home_team_id"], row["away_team_id"]):
            continue
        row["opponent_id"] = row["away_team_id"] if row["team_id"] == row["home_team_id"] else row["home_team_id"]
        # College box scores do not supply true targets or charted scoring chances.
        if row["provider"] != "nflverse":
            row.update(targets=None, gl_carries=None, rz_targets=None)
        row["position"] = "RB" if row["position"] == "FB" else row["position"]
        rows.append(row)
    # Schedule providers may save two IDs for the same real game. Count each
    # player's appearance once, and use one canonical ID for team totals.
    game_ids = defaultdict(lambda: defaultdict(int))
    for r in rows:
        key = (r["kickoff_utc"].date(), tuple(sorted((r["home_team_id"], r["away_team_id"]))))
        game_ids[key][r["game_id"]] += 2 if r["provider"] in {"nflverse", "cfbd"} else 1
    primary = {key: max(ids, key=lambda gid: (ids[gid], gid)) for key, ids in game_ids.items()}
    unique = {}
    for r in rows:
        key = (r["kickoff_utc"].date(), tuple(sorted((r["home_team_id"], r["away_team_id"]))))
        r["game_id"] = primary[key]
        pk = (r["game_id"], r["player_id"])
        if pk not in unique or r["provider"] in {"nflverse", "cfbd"}:
            unique[pk] = r
    return sorted(unique.values(), key=lambda r: (r["kickoff_utc"], r["game_id"], r["player_id"]))


def _mean(rows, key, default=0.0):
    pairs = [(0.8 ** i, r.get(key)) for i, r in enumerate(reversed(rows)) if r.get(key) is not None]
    return sum(w * float(v) for w, v in pairs) / sum(w for w, _ in pairs) if pairs else default


class OpportunityHistory:
    def __init__(self, records):
        self.players = defaultdict(list)
        self.teams = defaultdict(list)
        self.defenses = defaultdict(list)
        games = defaultdict(list)
        for r in records:
            self.players[(r["player_id"], r["team_id"])].append(r)
            games[(r["game_id"], r["team_id"])].append(r)
        for (_, team), players in games.items():
            first = players[0]
            totals = {k: sum(float(p.get(k) or 0) for p in players) for k in
                      ("carries", "targets", "receptions", "rush_td", "rec_td", "gl_carries", "rz_targets")}
            totals.update(kickoff_utc=first["kickoff_utc"], game_id=first["game_id"])
            for pos in ("QB", "RB", "WR", "TE"):
                totals[pos] = sum(float(p["rush_td"] or 0) + float(p["rec_td"] or 0)
                                  for p in players if p["position"] == pos)
            self.teams[team].append(totals)
            self.defenses[first["opponent_id"]].append(totals)
        for collection in (self.players, self.teams, self.defenses):
            for rows in collection.values():
                rows.sort(key=lambda r: r["kickoff_utc"])

    @staticmethod
    def recent(rows, cutoff):
        # Separate seasons: transfers and offseason roles never inherit a previous job.
        season_start = cutoff.replace(month=7, day=1, hour=0, minute=0, second=0, microsecond=0)
        if cutoff.month < 7:
            season_start = season_start.replace(year=season_start.year - 1)
        return [r for r in rows if season_start <= r["kickoff_utc"] < cutoff - timedelta(hours=6)][-6:]

    def features(self, player, team, opponent, position, cutoff, is_home):
        own = self.recent(self.players.get((player, team), []), cutoff)
        offense = self.recent(self.teams.get(team, []), cutoff)
        defense = self.recent(self.defenses.get(opponent, []), cutoff)
        if len(own) < 2 or len(offense) < 2 or len(defense) < 2:
            return None
        f = {k: _mean(own, k) for k in FEATURES[:7]}
        f.update(carry_trend=_mean(own[-2:], "carries") - _mean(own, "carries"),
                 receiving_trend=_mean(own[-2:], "receptions") - _mean(own, "receptions"),
                 team_rush_td=_mean(offense, "rush_td"), team_rec_td=_mean(offense, "rec_td"),
                 opp_rush_td=_mean(defense, "rush_td"), opp_rec_td=_mean(defense, "rec_td"),
                 opp_position_td=_mean(defense, position), games=len(own), is_home=float(is_home),
                 charting_known=float(any(r.get("gl_carries") is not None for r in own)),
                 pos_qb=float(position == "QB"), pos_rb=float(position in {"RB", "FB"}),
                 pos_te=float(position == "TE"))
        for dest, source in (("carry_share", "carries"), ("receiving_share", "receptions"),
                             ("goal_line_share", "gl_carries"), ("red_zone_share", "rz_targets")):
            f[dest] = min(1.0, _mean(own, source) / max(_mean(offense, source), 1.0))
        f["last_played_at"] = own[-1]["kickoff_utc"].isoformat()
        f["opponent_games"] = len(defense)
        f["last_game_carries"] = float(own[-1].get("carries") or 0)
        f["last_game_receptions"] = float(own[-1].get("receptions") or 0)
        return f


def vector(features):
    return np.array([float(features.get(k) or 0) for k in FEATURES])


def predict(model, features):
    x = (vector(features) - np.array(model["center"])) / np.array(model["scale"])
    logit = float(model["intercept"]) + float(x @ np.array(model["coefficients"]))
    logit = model.get("calibration_intercept", 0) + model.get("calibration_slope", 1) * logit
    return 1.0 / (1.0 + math.exp(-max(-25.0, min(25.0, logit))))


def artifact_path(league):
    return Path(settings.parquet_dir) / "models" / f"{league}_{VERSION}.json"


def load_model(league):
    try:
        body = artifact_path(league).read_bytes()
        model = json.loads(body)
        if model.get("features") != FEATURES or not model.get("research_approved"):
            return None
        model["artifact_sha256"] = hashlib.sha256(body).hexdigest()
        return model
    except (OSError, ValueError):
        return None


def explain(f, league):
    reasons = [f"Recent workload: {f['carries']:.1f} carries and {f['receptions']:.1f} receptions per game over {int(f['games'])} games."]
    if f["charting_known"]:
        reasons.append(f"Scoring-area work: {f['gl_carries']:.1f} carries inside the five and {f['rz_targets']:.1f} red-zone targets per game.")
        if f["goal_line_share"] > 0:
            reasons.append(f"{f['goal_line_share']:.0%} of the team's measured carries inside the five.")
    reasons.append(f"Team scoring: {f['team_rush_td'] + f['team_rec_td']:.1f} offensive TDs per game recently.")
    reasons.append(f"Opponent has allowed {f['opp_position_td']:.1f} TDs per game to this position over {int(f['opponent_games'])} games.")
    risks = ["Route participation, end-zone targets, and coverage splits are unavailable."]
    if league == "fbs" or not f["charting_known"]:
        risks.append("Goal-line and red-zone player opportunities are unavailable.")
    if f["carry_trend"] < -3 or f["receiving_trend"] < -1:
        risks.append("Recent workload is below the player's six-game average.")
    return reasons, risks
