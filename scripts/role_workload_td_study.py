"""Test a role-and-workload Anytime TD candidate without shipping it.

Development uses 2018-2022. The backup shrink and the shortlist cutoff are
chosen on 2023 only. 2024 is scored once and is not used to change a choice.
2025 and 2026 are not loaded. Frozen model files and live predictions are
not written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from spe.models.player_td.role_workload import (
    ROLE_BACKUP,
    ROLE_ROTATIONAL,
    ROLE_STARTER,
    ROLE_UNKNOWN,
    empirical_role_bases,
    metric_block,
    score_current_share,
    score_historical_rate,
    score_role_workload,
)

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache" / "nflverse"
REPORT = ROOT / "data" / "player_td_dev" / "role_workload_td_report.json"
FIT_MAX = 2022
SELECT_YEAR = 2023
FINAL_YEAR = 2024
SKILL = {"QB", "RB", "WR", "TE"}
PROTECTED = (
    ROOT / "data" / "parquet" / "models" / "ats" / "model_nfl.json",
    ROOT / "data" / "parquet" / "models" / "ats" / "model_fbs.json",
    ROOT / "data" / "parquet" / "models" / "nfl_player_td_v1.json",
    ROOT / "data" / "parquet" / "models" / "fbs_player_td_v1.json",
)


def _stamps() -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for path in PROTECTED:
        out[str(path)] = path.stat().st_mtime_ns if path.exists() else None
    return out


def _role(depth: float) -> str:
    if not np.isfinite(depth):
        return ROLE_UNKNOWN
    if depth <= 1:
        return ROLE_STARTER
    if depth <= 2:
        return ROLE_ROTATIONAL
    return ROLE_BACKUP


def _opportunity_counts() -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for year in range(2018, FINAL_YEAR + 1):
        path = CACHE / f"play_by_play_{year}.parquet"
        frame = pd.read_parquet(
            path,
            columns=["game_id", "week", "season_type", "yardline_100", "rush_attempt", "rusher_player_id", "receiver_player_id"],
        )
        frame = frame[frame["season_type"].astype(str).eq("REG")]
        yard = pd.to_numeric(frame["yardline_100"], errors="coerce")
        rush = pd.to_numeric(frame["rush_attempt"], errors="coerce").fillna(0).gt(0)
        gl = frame.loc[rush & yard.le(2) & frame["rusher_player_id"].notna(), ["game_id", "rusher_player_id"]]
        gl = gl.groupby(["game_id", "rusher_player_id"], as_index=False).size().rename(
            columns={"rusher_player_id": "player_id", "size": "gl_carries"}
        )
        rz = frame.loc[~rush & yard.le(20) & frame["receiver_player_id"].notna(), ["game_id", "receiver_player_id"]]
        rz = rz.groupby(["game_id", "receiver_player_id"], as_index=False).size().rename(
            columns={"receiver_player_id": "player_id", "size": "rz_targets"}
        )
        parts.append(gl.merge(rz, on=["game_id", "player_id"], how="outer"))
        print(f"opportunity {year}", flush=True)
    out = pd.concat(parts, ignore_index=True)
    return out.groupby(["game_id", "player_id"], as_index=False)[["gl_carries", "rz_targets"]].sum()


def _depth_roles() -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for year in range(2018, FINAL_YEAR + 1):
        depth = pd.read_parquet(
            CACHE / f"depth_charts_{year}.parquet",
            columns=["season", "week", "game_type", "depth_team", "gsis_id", "position", "depth_position"],
        )
        if "game_type" in depth.columns:
            depth = depth[depth["game_type"].astype(str).isin(["REG", "reg"])]
        offense = depth["position"].isin(SKILL) | depth["depth_position"].isin(SKILL)
        depth = depth.loc[offense, ["season", "week", "gsis_id", "depth_team"]]
        depth["depth_team"] = pd.to_numeric(depth["depth_team"], errors="coerce")
        depth = depth.dropna(subset=["gsis_id", "depth_team"])
        depth = depth.groupby(["season", "week", "gsis_id"], as_index=False)["depth_team"].min()
        parts.append(depth)
    out = pd.concat(parts, ignore_index=True)
    out["role"] = out["depth_team"].map(_role)
    return out.rename(columns={"gsis_id": "player_id"})


def _player_weeks() -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    columns = [
        "player_id",
        "season",
        "week",
        "season_type",
        "game_id",
        "team",
        "opponent_team",
        "position_group",
        "carries",
        "targets",
        "rushing_tds",
        "receiving_tds",
    ]
    for year in range(2018, FINAL_YEAR + 1):
        week = pd.read_parquet(CACHE / f"stats_player_week_{year}.parquet", columns=columns)
        week = week[week["season_type"].astype(str).eq("REG") & week["position_group"].isin(SKILL)]
        parts.append(week)
    frame = pd.concat(parts, ignore_index=True)
    for column in ("carries", "targets", "rushing_tds", "receiving_tds"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    frame["anytime_td"] = ((frame["rushing_tds"] + frame["receiving_tds"]) > 0).astype(float)
    frame["worked"] = (frame["carries"] + frame["targets"] >= 5).astype(float)
    return frame


def _add_priors(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values(["player_id", "season", "week"]).copy()
    grouped = out.groupby("player_id", sort=False)
    for source, dest in (
        ("carries", "prior_carries"),
        ("targets", "prior_targets"),
        ("gl_carries", "prior_gl_carries"),
        ("rz_targets", "prior_rz_targets"),
        ("rushing_tds", "prior_rush_td"),
        ("receiving_tds", "prior_rec_td"),
        ("anytime_td", "prior_td_rate"),
    ):
        out[dest] = grouped[source].transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
    out["prior_games"] = grouped.cumcount()
    out["opportunity_games"] = grouped["worked"].transform(lambda s: s.shift(1).cumsum()).fillna(0.0)
    return out


def _team_environment(frame: pd.DataFrame, mix: float) -> pd.DataFrame:
    team = (
        frame.groupby(["season", "week", "team"], as_index=False)
        .agg(rush_td=("rushing_tds", "sum"), rec_td=("receiving_tds", "sum"), opponent_team=("opponent_team", "first"))
        .sort_values(["team", "season", "week"])
    )
    grouped = team.groupby("team", sort=False)
    team["prior_rush"] = grouped["rush_td"].transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
    team["prior_rec"] = grouped["rec_td"].transform(lambda s: s.shift(1).expanding(min_periods=1).mean())
    allowed = team[["season", "week", "opponent_team", "rush_td", "rec_td"]].rename(
        columns={"opponent_team": "team", "rush_td": "rush_allowed", "rec_td": "rec_allowed"}
    )
    allowed = allowed.sort_values(["team", "season", "week"])
    allowed_group = allowed.groupby("team", sort=False)
    allowed["prior_rush_allowed"] = allowed_group["rush_allowed"].transform(
        lambda s: s.shift(1).expanding(min_periods=1).mean()
    )
    allowed["prior_rec_allowed"] = allowed_group["rec_allowed"].transform(
        lambda s: s.shift(1).expanding(min_periods=1).mean()
    )
    team = team.merge(
        allowed[["season", "week", "team", "prior_rush_allowed", "prior_rec_allowed"]],
        on=["season", "week", "team"],
        how="left",
    )
    opponent = team[["season", "week", "team", "prior_rush_allowed", "prior_rec_allowed"]].rename(
        columns={
            "team": "opponent_team",
            "prior_rush_allowed": "opp_rush_allowed",
            "prior_rec_allowed": "opp_rec_allowed",
        }
    )
    team = team.merge(opponent, on=["season", "week", "opponent_team"], how="left")
    own_rush = team["prior_rush"].fillna(team["prior_rush"].median())
    own_rec = team["prior_rec"].fillna(team["prior_rec"].median())
    opp_rush = team["opp_rush_allowed"].fillna(own_rush)
    opp_rec = team["opp_rec_allowed"].fillna(own_rec)
    team["team_lambda_rush_td"] = mix * own_rush + (1.0 - mix) * opp_rush
    team["team_lambda_pass_td"] = mix * own_rec + (1.0 - mix) * opp_rec
    return team[["season", "week", "team", "team_lambda_rush_td", "team_lambda_pass_td"]]


def _choose_mix(frame: pd.DataFrame) -> float:
    fit = frame[frame["season"] <= FIT_MAX]
    team = (
        fit.groupby(["season", "week", "team"], as_index=False)
        .agg(rush_td=("rushing_tds", "sum"), rec_td=("receiving_tds", "sum"))
    )
    best_mix = 0.5
    best_err = float("inf")
    for mix in (0.4, 0.5, 0.6, 0.7):
        env = _team_environment(fit, mix)
        joined = team.merge(env, on=["season", "week", "team"], how="inner")
        err = float(
            np.mean(np.abs(joined["rush_td"] - joined["team_lambda_rush_td"]) + np.abs(joined["rec_td"] - joined["team_lambda_pass_td"]))
        )
        if err < best_err:
            best_err = err
            best_mix = mix
    return best_mix


def _shortlist_table(frame: pd.DataFrame, probability: np.ndarray, threshold: float) -> dict[str, float | int]:
    work = frame.copy()
    work["p"] = probability
    work["y"] = work["y_atd"].to_numpy(dtype=float)
    kept = work[work["role"].isin([ROLE_STARTER, ROLE_ROTATIONAL]) & work["p"].ge(threshold)]
    top = work.sort_values("p", ascending=False).groupby("game_id", as_index=False).head(1)
    return {
        "threshold": threshold,
        "shortlist_n": int(len(kept)),
        "shortlist_hit_rate": float(kept["y"].mean()) if len(kept) else None,
        "always_top_n": int(len(top)),
        "always_top_hit_rate": float(top["y"].mean()) if len(top) else None,
    }


def _choose_threshold(frame: pd.DataFrame, probability: np.ndarray) -> dict[str, float | int | None | str]:
    best: dict[str, float | int | None | str] | None = None
    for threshold in (0.25, 0.35, 0.45, 0.55):
        row = _shortlist_table(frame, probability, threshold)
        if row["shortlist_n"] < 200 or row["shortlist_hit_rate"] is None or row["always_top_hit_rate"] is None:
            continue
        lift = float(row["shortlist_hit_rate"]) - float(row["always_top_hit_rate"])
        if best is None or lift > float(best["lift"]):
            best = {**row, "lift": lift}
    if best is None:
        return {"threshold": None, "lift": None, "note": "no cutoff on 2023 beat naming the top chance in every game"}
    if float(best["lift"]) <= 0:
        return {**best, "note": "every 2023 cutoff hit less often than naming the top chance in every game"}
    return {**best, "note": "cutoff chosen on 2023 only"}


def build_frame() -> pd.DataFrame:
    print("loading weeks", flush=True)
    frame = _player_weeks()
    print("loading goal line and red zone", flush=True)
    opportunity = _opportunity_counts()
    frame = frame.merge(opportunity, on=["game_id", "player_id"], how="left")
    frame["gl_carries"] = frame["gl_carries"].fillna(0.0)
    frame["rz_targets"] = frame["rz_targets"].fillna(0.0)
    print("loading depth", flush=True)
    frame = frame.merge(_depth_roles(), on=["season", "week", "player_id"], how="left")
    frame["role"] = frame["role"].fillna(ROLE_UNKNOWN)
    frame = _add_priors(frame)
    frame["y_atd"] = frame["anytime_td"]
    frame["team_id"] = frame["team"]
    return frame[frame["prior_games"] >= 1].reset_index(drop=True)


def run() -> dict:
    before = _stamps()
    frame = build_frame()
    mix = _choose_mix(frame)
    env = _team_environment(frame, mix)
    frame = frame.merge(env, on=["season", "week", "team"], how="left")
    fit = frame[frame["season"] <= FIT_MAX].copy()
    select = frame[frame["season"] == SELECT_YEAR].copy()
    final = frame[frame["season"] == FINAL_YEAR].copy()
    bases = empirical_role_bases(fit)
    best_gl = 2.5
    best_brier = float("inf")
    for goal_line_weight in (1.0, 2.5, 4.0):
        probability = score_role_workload(fit, bases, goal_line_weight=goal_line_weight, backup_games=8.0)
        score = metric_block(fit["y_atd"].to_numpy(dtype=float), probability)["brier"]
        if score < best_brier:
            best_brier = score
            best_gl = goal_line_weight
    best_k = 8.0
    best_select = float("inf")
    for backup_games in (4.0, 8.0, 16.0):
        probability = score_role_workload(select, bases, goal_line_weight=best_gl, backup_games=backup_games)
        score = metric_block(select["y_atd"].to_numpy(dtype=float), probability)["brier"]
        if score < best_select:
            best_select = score
            best_k = backup_games
    select_p = score_role_workload(select, bases, goal_line_weight=best_gl, backup_games=best_k)
    threshold = _choose_threshold(select, select_p)
    final_y = final["y_atd"].to_numpy(dtype=float)
    final_candidate = score_role_workload(final, bases, goal_line_weight=best_gl, backup_games=best_k)
    final_share = score_current_share(final)
    final_rate = score_historical_rate(final)
    by_role = {}
    for role, part in final.groupby("role"):
        mask = final["role"].eq(role).to_numpy()
        by_role[str(role)] = {
            "candidate": metric_block(final_y[mask], final_candidate[mask]),
            "current_share": metric_block(final_y[mask], final_share[mask]),
            "historical_rate": metric_block(final_y[mask], final_rate[mask]),
        }
    untouched = {
        "candidate": metric_block(final_y, final_candidate),
        "current_share": metric_block(final_y, final_share),
        "historical_rate": metric_block(final_y, final_rate),
        "by_role": by_role,
    }
    candidate_brier = untouched["candidate"]["brier"]
    share_brier = untouched["current_share"]["brier"]
    beats_share = bool(candidate_brier < share_brier)
    shortlist_final = None
    if threshold.get("threshold") is not None and float(threshold.get("lift") or 0) > 0:
        shortlist_final = _shortlist_table(final, final_candidate, float(threshold["threshold"]))
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "league": "nfl",
        "promoted": False,
        "play_enabled": False,
        "frozen_predictions_changed": False,
        "protected_artifacts_unchanged": _stamps() == before,
        "windows": {
            "develop_team_mix_and_goal_line": "2018-2022",
            "choose_backup_shrink_and_shortlist": str(SELECT_YEAR),
            "untouched_final": str(FINAL_YEAR),
            "not_loaded": [2025, 2026],
        },
        "choices_frozen_before_final": {
            "team_mix": mix,
            "goal_line_weight": best_gl,
            "backup_shrink_games": best_k,
            "historical_td_weight": 0.4,
            "shortlist": threshold,
        },
        "untouched_2024": untouched,
        "beats_current_share_on_untouched_2024": beats_share,
        "ship_candidate": False,
        "ship_reason": (
            "Better than the current share formula on 2024, and still not shipped. Frozen predictions stay as they are."
            if beats_share
            else "The candidate did not beat the current share formula on the untouched 2024 games."
        ),
        "shortlist_on_untouched_2024": shortlist_final,
        "shortlist_note": "Fewer names are not treated as better. The cutoff was chosen on 2023. A higher hit rate on 2024 is a result, not a new rule for the live page.",
        "price_shortlist": "Not tested. These files have no anytime-touchdown prices, so a fresh price cannot be judged here.",
        "college": "Not in this test. College red-zone and goal-line counts are still missing, and a missing injury is not treated as healthy.",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if _stamps() != before:
        raise RuntimeError("A protected model file changed during the study")
    print(json.dumps({"report": str(REPORT), "beats_share": beats_share, "brier": candidate_brier}, indent=2), flush=True)
    return report


if __name__ == "__main__":
    run()
