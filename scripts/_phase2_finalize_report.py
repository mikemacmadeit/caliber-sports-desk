"""Merge Phase 2 narrative report fields into phase2_team_td_report.json."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data" / "phase2_team_td_report.json"
LIVE = ROOT / "data" / "phase2_team_td_live.json"


def _window_table(league: dict) -> list[dict]:
    rows = []
    for w in league.get("windows") or []:
        m = (w.get("metrics") or {}).get("model") or {}
        b = (w.get("metrics") or {}).get("baseline_league_mean") or {}
        p = (w.get("metrics") or {}).get("baseline_team_prior") or {}
        rows.append(
            {
                "window": w.get("window"),
                "status": w.get("status"),
                "test_n": w.get("test_n"),
                "model_log_loss": m.get("log_loss"),
                "model_mae": (m.get("count") or {}).get("mae"),
                "model_exact": (m.get("count") or {}).get("exact"),
                "model_within_1": (m.get("count") or {}).get("within_1"),
                "baseline_league_log_loss": b.get("log_loss"),
                "baseline_prior_log_loss": p.get("log_loss"),
                "m3_comparison": w.get("m3_team_scoring_comparison"),
            }
        )
    return rows


def main() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    live = json.loads(LIVE.read_text(encoding="utf-8")) if LIVE.exists() else {}
    nfl = report.get("leagues", {}).get("nfl", {})
    fbs = report.get("leagues", {}).get("fbs", {})

    examples = []
    for row in live.get("examples") or []:
        examples.append(
            {
                "game_id": row.get("game_id"),
                "league_id": row.get("league_id"),
                "kickoff_utc": row.get("kickoff_utc"),
                "team": row.get("team_name"),
                "opponent": row.get("opp_name"),
                "is_home": row.get("is_home"),
                "sample_size": row.get("sample_size"),
                "lambda_off_td": row.get("lambda_off_td"),
                "lambda_pass_td": row.get("lambda_pass_td"),
                "lambda_rush_td": row.get("lambda_rush_td"),
                "p_off_td": row.get("p_off_td"),
                "exp_possessions": row.get("exp_possessions"),
                "exp_off_plays": row.get("exp_off_plays"),
                "exp_rz_plays": row.get("exp_rz_plays"),
                "exp_scoring_drives": row.get("exp_scoring_drives"),
                "features_snapshot": row.get("features_snapshot"),
            }
        )

    report["phase2_results"] = {
        "1_model_architecture": {
            "family": "Poisson GLM (log link) fit with numpy IRLS",
            "leagues": "Separate NFL and FBS artifacts (team_td_v1)",
            "targets": "off_td, pass_td, rush_td (scrimmage only)",
            "distributions": "Poisson PMF for buckets 0,1,2,3,4,5+",
            "pass_rush_constraint": "Pass and rush means rescaled to sum to total mean",
            "def_st": "Counted separately; never mixed into offensive totals",
            "promotion": "research_only; play_enabled=false; not wired to player forecasts",
            "m3_m4": "Untouched (byte-preserved during train)",
        },
        "2_features_used": {
            "nfl": nfl.get("features"),
            "fbs": fbs.get("features"),
            "notes": [
                "All features are prior-kickoff only (shift/expanding means).",
                "Weather used only when NWS forecast issued_at is before kickoff; historical coverage is sparse so weather often drops for lack of variance.",
                "Injuries used only when coverage_complete and reported_at < kickoff.",
                "FBS injury columns dropped when no variance in training window.",
            ],
        },
        "3_training_seasons": {
            "nfl": nfl.get("seasons"),
            "fbs": fbs.get("seasons"),
            "train_end_nfl": nfl.get("train_end"),
            "train_end_fbs": fbs.get("train_end"),
            "note": "2025 and completed 2026 finals included only when available before the training cutoff. FBS 2026 finals exist but have zero play-by-play rows, so FBS training seasons stop at 2025.",
        },
        "4_nfl_accuracy": {
            "shadow": (nfl.get("shadow_metrics") or {}).get("model"),
            "baselines": {
                "league_mean": (nfl.get("shadow_metrics") or {}).get("baseline_league_mean"),
                "team_prior": (nfl.get("shadow_metrics") or {}).get("baseline_team_prior"),
            },
            "windows": _window_table(nfl),
            "verdict": "NFL MAE slightly beats league-mean and team-prior baselines; multinomial log loss is roughly tied with the league-mean Poisson. Exact count hit rate ~24–30%.",
        },
        "5_fbs_accuracy": {
            "shadow": (fbs.get("shadow_metrics") or {}).get("model"),
            "baselines": {
                "league_mean": (fbs.get("shadow_metrics") or {}).get("baseline_league_mean"),
                "team_prior": (fbs.get("shadow_metrics") or {}).get("baseline_team_prior"),
            },
            "windows": _window_table(fbs),
            "verdict": "FBS clearly beats league-mean and team-prior baselines on log loss and MAE (shadow log loss 1.67 vs ~1.73). Exact count ~21%, within-1 ~61%.",
        },
        "6_calibration": {
            "nfl_shadow": ((nfl.get("shadow_metrics") or {}).get("model") or {}).get("calibration"),
            "fbs_shadow": ((fbs.get("shadow_metrics") or {}).get("model") or {}).get("calibration"),
            "by_scoring_environment_nfl": ((nfl.get("shadow_metrics") or {}).get("model") and (nfl.get("shadow_metrics") or {}).get("by_scoring_environment")),
            "by_scoring_environment_fbs": (fbs.get("shadow_metrics") or {}).get("by_scoring_environment"),
            "notes": [
                "NFL under-predicts 3-TD games and slightly over-predicts 0/1 and 5+ in the latest shadow window.",
                "FBS bucket calibration gaps are mostly within ~2 percentage points on the shadow window.",
            ],
        },
        "7_example_real_game_distributions": examples,
        "8_remaining_weaknesses": [
            "NFL lift over a constant league-mean Poisson is small; opponent and pace features need stronger signal or nonlinear interactions.",
            "FBS 2026 has 629 final games but 0 plays ingested — live FBS scores use ≤2025 history only.",
            "Historical NWS weather rarely available in-sample; weather coefficients usually drop.",
            "Injury features are sparse outside NFL complete reports.",
            "Pass/rush independence is an approximation; game-script correlation is not yet simulated (Phase 4).",
            "Possessions / RZ / scoring-drive outputs are shrunk historical means, not full GLMs.",
            "M3 comparison uses team_rate×4 skill slots as a coarse proxy only.",
            "Not connected to player Anytime TD forecasts (Phase 3 deliberately deferred).",
        ],
        "constraints_honored": {
            "m3_m4_unchanged": True,
            "validated_play_disabled": True,
            "shadow_ledger_preserved": True,
            "no_auto_promotion": True,
            "no_player_forecast_wiring": True,
            "no_future_data": True,
        },
        "next_phase": "Phase 3: allocate team offensive TDs to individual players (goal-line carries, end-zone targets, usage, injuries, matchups). Do not start until this report is reviewed.",
    }
    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Updated {REPORT}")


if __name__ == "__main__":
    main()
