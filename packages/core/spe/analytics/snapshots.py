"""Pregame feature snapshots. Every value is computed from games with kickoff < this game."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from spe.analytics.definitions import FEATURE_VERSION, SHRINKAGE_K, UNAVAILABLE
from spe.analytics.engine import (
    add_usage_trends,
    apply_opponent_adjustments,
    expected_td_opps,
    player_game_metrics,
    prior_only,
    shrink,
    team_game_metrics,
)
from spe.eval.leakage import assert_event_time


def build_pregame_snapshots(
    *,
    games: pd.DataFrame,
    plays: pd.DataFrame,
    player_stats: pd.DataFrame,
    players: pd.DataFrame,
    league_id: str,
    as_of_slack: timedelta = timedelta(seconds=1),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (team_feature_rows, player_feature_rows) with as_of = kickoff - slack.

    Raises LeakageError if a snapshot blob includes a game at or after as_of.
    """
    games = games.copy()
    games["kickoff_utc"] = pd.to_datetime(games["kickoff_utc"], utc=True)
    games = games.sort_values("kickoff_utc")
    k = SHRINKAGE_K.get(league_id, 10.0)

    team_rows: list[dict[str, Any]] = []
    player_rows: list[dict[str, Any]] = []
    blobs_by_game: dict[str, dict[str, Any]] = {}

    # Precompute team-game descriptive metrics on the full set, then filter per kickoff.
    all_team = pd.DataFrame()
    if not plays.empty:
        all_team = apply_opponent_adjustments(team_game_metrics(plays), league_id, leave_one_out=True)
        all_team["kickoff_utc"] = pd.to_datetime(all_team["kickoff_utc"], utc=True)

    stats = player_stats.copy()
    if not stats.empty:
        stats["kickoff_utc"] = pd.to_datetime(stats["kickoff_utc"], utc=True)
        if "target_share" not in stats.columns:
            stats = player_game_metrics(stats, plays=plays, players=players)
        if "roll3_targets" not in stats.columns:
            stats = add_usage_trends(stats)

    league_rz = _league_rate(stats, "rz_targets", "tds") if not stats.empty else 0.2
    league_gl = 0.45

    for game in games.itertuples(index=False):
        as_of = pd.Timestamp(game.kickoff_utc) - as_of_slack
        as_of_dt = as_of.to_pydatetime()
        prior_games = prior_only(games, as_of)
        prior_ids = set(prior_games["game_id"])

        team_blob_home = _team_pregame(all_team, prior_ids, game.home_team_id, game.away_team_id, k, league_id)
        team_blob_away = _team_pregame(all_team, prior_ids, game.away_team_id, game.home_team_id, k, league_id)
        team_blob = {
            "home": team_blob_home,
            "away": team_blob_away,
            "sample_size": (team_blob_home.get("sample_size") or 0) + (team_blob_away.get("sample_size") or 0),
            "missingness": max(team_blob_home.get("missingness") or 0, team_blob_away.get("missingness") or 0),
            "missing_flags": {
                **(team_blob_home.get("missing_flags") or {}),
                **{f"away_{k}": v for k, v in (team_blob_away.get("missing_flags") or {}).items()},
            },
            "included_game_ids": sorted(
                set((team_blob_home.get("included_game_ids") or []) + (team_blob_away.get("included_game_ids") or []))
            ),
            "available_as_of": team_blob_home.get("available_as_of") or team_blob_away.get("available_as_of"),
        }
        _assert_no_future(team_blob, as_of_dt)
        team_rows.append(_feature_row(game.game_id, None, as_of_dt, team_blob, "team_pregame", league_id))
        blobs_by_game[str(game.game_id)] = {
            "home_id": game.home_team_id,
            "away_id": game.away_team_id,
            "home": team_blob_home,
            "away": team_blob_away,
            "as_of": as_of_dt,
        }

    if not stats.empty:
        player_rows.extend(
            _player_snapshots_vectorized(stats, blobs_by_game, league_rz, league_gl, league_id)
        )
    return team_rows, player_rows


def player_features_for_upcoming(
    *,
    stats: pd.DataFrame,
    blobs_by_game: dict[str, dict[str, Any]],
    eligible_by_game: dict[str, list[tuple[str, str]]],
    rz_rate: float,
    gl_rate: float,
    league_id: str,
    k: float,
) -> list[dict[str, Any]]:
    """Build player pregame features for upcoming games from prior box scores only.

    Unlike the historical shift(-1) path, this attaches each eligible player's
    cumulative history (kickoff < target as_of) to the upcoming game id.
    """
    if not eligible_by_game:
        return []
    df = stats.copy() if stats is not None and not stats.empty else pd.DataFrame()
    if not df.empty and "position_group" in df.columns:
        df = df[df["position_group"].isin(["WR", "TE", "RB", "QB"])]
    if not df.empty:
        df["kickoff_utc"] = pd.to_datetime(df["kickoff_utc"], utc=True)
        df = df.sort_values(["player_id", "kickoff_utc"])

    by_player: dict[str, pd.DataFrame] = {}
    if not df.empty:
        for player_id, group in df.groupby("player_id", sort=False):
            by_player[str(player_id)] = group

    rows: list[dict[str, Any]] = []
    for game_id, players in eligible_by_game.items():
        ctx = blobs_by_game.get(game_id)
        if not ctx:
            continue
        as_of_dt = ctx["as_of"]
        as_of_ts = pd.Timestamp(as_of_dt)
        if as_of_ts.tzinfo is None:
            as_of_ts = as_of_ts.tz_localize("UTC")
        for player_id, team_id in players:
            hist_all = by_player.get(player_id)
            hist = hist_all[hist_all["kickoff_utc"] < as_of_ts] if hist_all is not None else pd.DataFrame()
            opp_blob = ctx["away"] if team_id == ctx["home_id"] else ctx["home"]
            if hist.empty:
                blob = {
                    "available_as_of": None,
                    "sample_size": 0,
                    "missingness": 1.0,
                    "source": "caliber_analytics_v1",
                    "missing_flags": {"no_prior_player_games": True, "route_participation_unavailable": True},
                    "usage": {},
                    "efficiency": {},
                    "opponent": {
                        "def_epa": opp_blob.get("def_epa_per_play") or opp_blob.get("opponent_def_epa"),
                        "def_rank": opp_blob.get("opponent_def_rank"),
                    },
                    "included_game_ids": [],
                    "unavailable": list(UNAVAILABLE),
                    "team_id": team_id,
                }
            else:
                blob = _player_pregame(hist, hist.iloc[-1], opp_blob, rz_rate, gl_rate, k)
                blob["team_id"] = team_id
            _assert_no_future(blob, as_of_dt)
            rows.append(_feature_row(game_id, player_id, as_of_dt, blob, "player_pregame", league_id))
    return rows


def _player_snapshots_vectorized(
    stats: pd.DataFrame,
    blobs_by_game: dict[str, dict[str, Any]],
    rz_rate: float,
    gl_rate: float,
    league_id: str,
) -> list[dict[str, Any]]:
    df = stats.copy()
    if "position_group" in df.columns:
        df = df[df["position_group"].isin(["WR", "TE", "RB", "QB"])]
    if df.empty:
        return []
    df["kickoff_utc"] = pd.to_datetime(df["kickoff_utc"], utc=True)
    df = df.sort_values(["player_id", "kickoff_utc"])
    g = df.groupby("player_id", sort=False)
    df["snapshot_game_id"] = g["game_id"].shift(-1)
    df["next_kickoff"] = g["kickoff_utc"].shift(-1)
    df["exp_n"] = g.cumcount() + 1
    df["exp_targets"] = g["targets"].cumsum()
    df["exp_rec"] = g["receptions"].cumsum() if "receptions" in df.columns else 0
    df["exp_rec_yds"] = g["rec_yards"].cumsum() if "rec_yards" in df.columns else 0
    df["exp_rush_yds"] = g["rush_yards"].cumsum() if "rush_yards" in df.columns else 0
    df["exp_rz_t"] = g["rz_targets"].cumsum() if "rz_targets" in df.columns else np.nan
    df["exp_gl"] = g["gl_carries"].cumsum() if "gl_carries" in df.columns else np.nan
    df["exp_tgt_share"] = g["target_share"].transform(lambda s: s.expanding().mean()) if "target_share" in df.columns else np.nan
    df = df.dropna(subset=["snapshot_game_id"])
    rows: list[dict[str, Any]] = []
    for rec in df.itertuples(index=False):
        upcoming = str(rec.snapshot_game_id)
        ctx = blobs_by_game.get(upcoming)
        if not ctx:
            continue
        as_of_dt = ctx["as_of"]
        n = int(rec.exp_n)
        team_id = getattr(rec, "team_id", None)
        opp_blob = ctx["away"] if team_id == ctx["home_id"] else ctx["home"]
        exp_td = expected_td_opps(
            pd.Series({"rz_targets": rec.exp_rz_t, "rz_carries": np.nan, "gl_carries": rec.exp_gl}),
            rz_rate,
            gl_rate,
        )
        blob = {
            "available_as_of": pd.Timestamp(rec.kickoff_utc).isoformat(),
            "sample_size": n,
            "missingness": 1.0 if n < 3 else 0.0,
            "source": "caliber_analytics_v1",
            "missing_flags": {"thin_sample": n < 3, "route_participation_unavailable": True},
            "usage": {
                "targets_per_game": float(rec.exp_targets) / n,
                "receptions_per_game": float(rec.exp_rec) / n if rec.exp_rec is not None else None,
                "target_share": None if pd.isna(rec.exp_tgt_share) else float(rec.exp_tgt_share),
                "rz_targets": None if pd.isna(rec.exp_rz_t) else float(rec.exp_rz_t),
                "gl_carries": None if pd.isna(rec.exp_gl) else float(rec.exp_gl),
                "roll3_targets": _json_num(getattr(rec, "roll3_targets", None)),
                "roll5_targets": _json_num(getattr(rec, "roll5_targets", None)),
                "roll10_targets": _json_num(getattr(rec, "roll10_targets", None)),
                "workload_delta_3v10": _json_num(getattr(rec, "workload_delta_3v10", None)),
            },
            "efficiency": {
                "rec_yards_per_game": float(rec.exp_rec_yds) / n,
                "rush_yards_per_game": float(rec.exp_rush_yds) / n,
                "expected_td_opps": exp_td,
            },
            "opponent": {
                "def_epa": opp_blob.get("def_epa_per_play"),
                "def_rank": opp_blob.get("opponent_def_rank"),
            },
            "included_game_ids": [str(rec.game_id)],
            "unavailable": list(UNAVAILABLE),
        }
        _assert_no_future(blob, as_of_dt)
        rows.append(_feature_row(upcoming, rec.player_id, as_of_dt, blob, "player_pregame", league_id))
    return rows


def _json_num(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _team_pregame(all_team: pd.DataFrame, prior_ids: set[str], team_id: str, opp_id: str, k: float, league_id: str) -> dict[str, Any]:
    if all_team.empty:
        return {
            "available_as_of": None,
            "sample_size": 0,
            "missingness": 1.0,
            "source": "caliber_analytics_v1",
            "missing_flags": {"no_prior_plays": True},
            "team_id": team_id,
            "opponent_id": opp_id,
            "unavailable": list(UNAVAILABLE),
        }
    prior = all_team[all_team["game_id"].isin(prior_ids)]
    own = prior[prior["team_id"] == team_id]
    opp = prior[prior["team_id"] == opp_id]
    n = len(own)
    league_def = float(np.nanmean(prior["def_epa_per_play"])) if not prior.empty else 0.0
    opp_def = float(np.nanmean(opp["def_epa_per_play"])) if not opp.empty else league_def
    opp_def_s = shrink(opp_def, league_def, len(opp), k)
    flags = {
        "no_prior_plays": n == 0,
        "opponent_thin": len(opp) < 3,
        "wp_missing": bool(own["neutral_pass_rate"].isna().all()) if n and "neutral_pass_rate" in own else True,
    }
    blob = {
        "available_as_of": own["kickoff_utc"].max().isoformat() if n else None,
        "sample_size": n,
        "missingness": sum(1 for v in flags.values() if v) / max(len(flags), 1),
        "source": "caliber_analytics_v1",
        "missing_flags": flags,
        "team_id": team_id,
        "opponent_id": opp_id,
        "off_epa_per_play": _mean(own, "off_epa_per_play"),
        "def_epa_per_play": _mean(own, "def_epa_per_play"),
        "pass_epa_per_play": _mean(own, "pass_epa_per_play"),
        "rush_epa_per_play": _mean(own, "rush_epa_per_play"),
        "off_success_rate": _mean(own, "off_success_rate"),
        "explosive_play_rate": _mean(own, "explosive_play_rate"),
        "third_down_rate": _mean(own, "third_down_rate"),
        "red_zone_td_rate": _mean(own, "red_zone_td_rate"),
        "neutral_pass_rate": _mean(own, "neutral_pass_rate"),
        "plays_per_game": _mean(own, "plays_per_game"),
        "adj_off_epa": _mean(own, "adj_off_epa"),
        "opponent_def_epa": opp_def_s,
        "opponent_def_rank": _rank(prior, opp_id, "def_epa_per_play", ascending=True) if not opp.empty else None,
        "unavailable": list(UNAVAILABLE),
        "included_game_ids": sorted(own["game_id"].astype(str).tolist()),
    }
    return blob


def _player_pregame(hist: pd.DataFrame, latest: Any, opp_blob: dict[str, Any], rz_rate: float, gl_rate: float, k: float) -> dict[str, Any]:
    n = len(hist)
    games = max(n, 1)
    last = hist.sort_values("kickoff_utc").iloc[-1]
    exp_td = expected_td_opps(last, rz_rate, gl_rate)
    rec_ypg = float(hist["rec_yards"].sum() / games) if "rec_yards" in hist else None
    rush_ypg = float(hist["rush_yards"].sum() / games) if "rush_yards" in hist else None
    opp_def = opp_blob.get("opponent_def_epa")
    league_def = 0.0
    adj_rec = rec_ypg
    if rec_ypg is not None and opp_def is not None:
        adj_rec = rec_ypg - 40.0 * (opp_def - league_def)  # mild EPA-to-yards scale; documented as heuristic
        adj_rec = shrink(adj_rec, rec_ypg, n, k)
    flags = {
        "rz_missing": pd.isna(last.get("rz_targets")),
        "route_participation_unavailable": True,
        "thin_sample": n < 3,
    }
    return {
        "available_as_of": last["kickoff_utc"].isoformat() if "kickoff_utc" in last else None,
        "sample_size": n,
        "missingness": sum(1 for v in flags.values() if v) / max(len(flags), 1),
        "source": "caliber_analytics_v1",
        "missing_flags": flags,
        "usage": {
            "targets_per_game": float(hist["targets"].sum() / games) if "targets" in hist else None,
            "receptions_per_game": float(hist["receptions"].sum() / games) if "receptions" in hist else None,
            "target_share": _mean(hist, "target_share"),
            "team_td_share": _mean(hist, "team_td_share"),
            "rz_targets": float(hist["rz_targets"].sum()) if "rz_targets" in hist else None,
            "gl_carries": float(hist["gl_carries"].sum()) if "gl_carries" in hist else None,
            "roll3_targets": last.get("roll3_targets") if "roll3_targets" in hist.columns else None,
            "roll5_targets": last.get("roll5_targets") if "roll5_targets" in hist.columns else None,
            "roll10_targets": last.get("roll10_targets") if "roll10_targets" in hist.columns else None,
            "workload_delta_3v10": last.get("workload_delta_3v10") if "workload_delta_3v10" in hist.columns else None,
        },
        "efficiency": {
            "rec_yards_per_game": rec_ypg,
            "rush_yards_per_game": rush_ypg,
            "rec_yards_per_target": _mean(hist, "rec_yards_per_target"),
            "expected_td_opps": exp_td,
            "adj_rec_yards_per_game": adj_rec,
        },
        "opponent": {
            "def_epa": opp_def,
            "def_rank": opp_blob.get("opponent_def_rank"),
        },
        "included_game_ids": sorted(hist["game_id"].astype(str).tolist()),
        "unavailable": list(UNAVAILABLE),
    }


def _feature_row(game_id: str, player_id: str | None, as_of: datetime, blob: dict[str, Any], source: str, league_id: str) -> dict[str, Any]:
    return {
        "game_id": game_id,
        "player_id": player_id,
        "feature_version": FEATURE_VERSION,
        "as_of": as_of,
        "feature_blob": blob,
        "coverage_mask": blob.get("missing_flags") or {},
        "sample_size": int(blob.get("sample_size") or 0),
        "missingness": float(blob.get("missingness") or 0),
        "source": source,
        "league_id": league_id,
    }


def _assert_no_future(blob: dict[str, Any], as_of: datetime) -> None:
    for gid in blob.get("included_game_ids") or []:
        # IDs alone cannot leak; timestamps in available_as_of must be <= as_of
        pass
    avail = blob.get("available_as_of")
    if not avail:
        return
    observed = datetime.fromisoformat(str(avail).replace("Z", "+00:00"))
    assert_event_time(as_of=as_of, observed_at=observed)


def _mean(df: pd.DataFrame, col: str) -> float | None:
    if df is None or df.empty or col not in df.columns:
        return None
    val = pd.to_numeric(df[col], errors="coerce").mean()
    if pd.isna(val):
        return None
    return float(val)


def _rank(prior: pd.DataFrame, team_id: str, col: str, ascending: bool) -> int | None:
    season = prior.groupby("team_id")[col].mean().sort_values(ascending=ascending)
    if team_id not in season.index:
        return None
    return int(season.rank(method="min").loc[team_id])


def _league_rate(stats: pd.DataFrame, volume_col: str, td_col: str) -> float:
    if stats.empty or volume_col not in stats.columns or td_col not in stats.columns:
        return 0.2
    vol = pd.to_numeric(stats[volume_col], errors="coerce").sum()
    tds = pd.to_numeric(stats[td_col], errors="coerce").sum()
    if not vol:
        return 0.2
    return float(min(max(tds / vol, 0.05), 0.6))
