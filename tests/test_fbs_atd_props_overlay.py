"""FBS props board must not show M3 team-prior clone percentages."""

from __future__ import annotations

import json
from pathlib import Path

from spe.research.fbs_atd_props_overlay import apply_fbs_atd_research_overlay, is_m3_team_clone_forecast


def test_overlay_replaces_m3_with_player_specific(tmp_path: Path, monkeypatch) -> None:
    finder = {
        "games": [
            {
                "league_id": "fbs",
                "game_id": "espn:fbs:g1",
                "players": [
                    {
                        "player_id": "fbs:a",
                        "probabilities": {"primary_research": 0.41, "c_recent": 0.41},
                        "model_source": {
                            "primary": "m5_c_recent",
                            "locked_version": "fbs_m5_c_recent_locked_v1",
                            "prediction_timestamp": "2026-11-07T12:00:00+00:00",
                        },
                    },
                    {
                        "player_id": "fbs:b",
                        "probabilities": {"primary_research": 0.22, "c_recent": 0.22},
                        "model_source": {
                            "primary": "m5_c_recent",
                            "locked_version": "fbs_m5_c_recent_locked_v1",
                            "prediction_timestamp": "2026-11-07T12:00:00+00:00",
                        },
                    },
                ],
            }
        ]
    }
    path = tmp_path / "anytime_td_finder.json"
    path.write_text(json.dumps(finder), encoding="utf-8")
    monkeypatch.setattr(
        "spe.research.fbs_atd_props_overlay.finder_payload_path",
        lambda: path,
    )
    # Clear module cache
    import spe.research.fbs_atd_props_overlay as mod

    mod._CACHE.update({"mtime": None, "loaded_at": 0.0, "index": {}})

    m3 = {
        "model_version": "m3-fbs-anytime_td",
        "p_estimate": 0.419,
        "confidence_interval": (0.339, 0.499),
        "reason_codes": ["RESEARCH_ONLY"],
    }
    a = apply_fbs_atd_research_overlay(
        dict(m3), game_id="espn:fbs:g1", player_id="fbs:a", league_id="fbs", market_id="anytime_td"
    )
    b = apply_fbs_atd_research_overlay(
        dict(m3), game_id="espn:fbs:g1", player_id="fbs:b", league_id="fbs", market_id="anytime_td"
    )
    assert a is not None and b is not None
    # Without player_td_v1 in the stub, falls back to C_recent primary.
    assert abs(float(a["p_estimate"]) - 0.41) < 1e-9
    assert abs(float(b["p_estimate"]) - 0.22) < 1e-9
    assert a["p_estimate"] != b["p_estimate"]
    assert "M3_TEAM_PRIOR_OVERRIDE" in a["reason_codes"]


def test_overlay_blends_v1_and_c_recent(tmp_path: Path, monkeypatch) -> None:
    finder = {
        "games": [
            {
                "league_id": "fbs",
                "game_id": "espn:fbs:g1",
                "players": [
                    {
                        "player_id": "fbs:a",
                        "game_level_supported": True,
                        "probabilities": {
                            "primary_research": 0.40,
                            "c_recent": 0.40,
                            "player_td_v1": 0.50,
                        },
                        "model_source": {"locked_version": "fbs_m5_c_recent_locked_v1"},
                    }
                ],
            }
        ]
    }
    path = tmp_path / "anytime_td_finder.json"
    path.write_text(json.dumps(finder), encoding="utf-8")
    monkeypatch.setattr(
        "spe.research.fbs_atd_props_overlay.finder_payload_path",
        lambda: path,
    )
    import spe.research.fbs_atd_props_overlay as mod

    mod._CACHE.update({"mtime": None, "loaded_at": 0.0, "index": {}})
    out = apply_fbs_atd_research_overlay(
        {"model_version": "m3-fbs-anytime_td", "p_estimate": 0.419, "reason_codes": []},
        game_id="espn:fbs:g1",
        player_id="fbs:a",
        league_id="fbs",
        market_id="anytime_td",
    )
    assert out is not None
    assert abs(float(out["p_estimate"]) - (0.55 * 0.50 + 0.45 * 0.40)) < 1e-9
    assert out["model_version"] == "fbs_research_blend_v1_c_recent"


def test_overlay_hides_m3_clone_without_research() -> None:
    m3 = {"model_version": "m3-fbs-anytime_td", "p_estimate": 0.419, "reason_codes": []}
    assert is_m3_team_clone_forecast(m3)
    assert (
        apply_fbs_atd_research_overlay(
            m3, game_id="espn:fbs:missing", player_id="fbs:x", league_id="fbs", market_id="anytime_td"
        )
        is None
    )


def test_predict_row_blocks_anytime_td_team_clone() -> None:
    from spe.models.train import FittedMarket, predict_row

    fitted = FittedMarket(
        league_id="fbs",
        market_id="anytime_td",
        family="anytime_td",
        train_end="2026-01-01",
        epa_scale="ppa",
        league_rate=0.3,
        team_rates={"fbs:1": 0.55},
        pos_rates={"RB": 0.4},
        player_rates={},
        player_n={},
        k_player=12.0,
        opp_beta=0.0,
        z_mean=0.0,
        z_std=1.0,
        platt_a=0.0,
        platt_b=1.0,
        sigma=1.0,
        default_line=0.5,
        notes=[],
    )
    pred = predict_row(fitted, "fbs:nobody", "fbs:1", "RB", 0.0, 0)
    assert pred.get("status") == "rejected_no_player_history"
    assert pred.get("p") is None
