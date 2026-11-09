"""Regression: research bundle wiring, source conventions, isolated settlement grading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spe.models.ats.labels import grade_side
from spe.models.ats.research_bundle import CLEAN_PROSPECTIVE_COHORT, load_ats_research_bundle
from spe.models.ats.spread_contract import CURRENT_SPREAD_CONTRACT, to_home_handicap


def test_research_bundle_does_not_apply_calibrator_to_a_different_feature_list():
    art, bundle = load_ats_research_bundle("nfl")
    assert art is not None
    assert "home_qb_out" in art.feature_cols
    assert bundle.cover_probs_enabled is False
    assert any("different feature list" in note for note in bundle.notes)
    assert bundle.spread_contract == CURRENT_SPREAD_CONTRACT
    assert bundle.cohort_version == CLEAN_PROSPECTIVE_COHORT
    assert bundle.margin_artifact_sha256
    # Legacy Platt stays on disk and is stripped in memory. It was fit without injuries.
    prod = json.loads(Path(bundle.margin_artifact_path).read_text(encoding="utf-8"))
    assert abs(float(prod["platt_b"]) - 1.3708498538640321) < 1e-6
    assert art.platt_a == 0.0
    assert art.platt_b == 1.0
    assert CLEAN_PROSPECTIVE_COHORT in art.model_version_id
    assert bundle.play_enabled is False


def test_adapters_declare_conventions():
    assert to_home_handicap(6.5, source_kind="nflverse_margin_forecast") == -6.5
    assert to_home_handicap(-6.5, source_kind="sportsbook_handicap") == -6.5
    # Never blindly negate live book lines
    assert to_home_handicap(-3.0, source_kind="sportsbook_handicap") == -3.0


@pytest.mark.parametrize(
    "hs,aws,L,expect_home",
    [
        (34, 28, 6.5, "loss"),  # ARI@BUF home favorite fails to cover
        (10, 16, 8.0, "loss"),  # NE@CIN
        (28, 21, 7.0, "push"),
        (24, 21, -3.5, "cover"),  # home dog +3.5
        (17, 24, 0.0, "loss"),  # designated home on pick'em
        (27, 20, 3.0, "cover"),  # home favorite covers
    ],
)
def test_isolated_grading_favorites_dogs_pushes_neutral(hs, aws, L, expect_home):
    h = to_home_handicap(L, source_kind="nflverse_margin_forecast")
    assert grade_side(hs - aws, h, "home") == expect_home
    if expect_home == "push":
        assert grade_side(hs - aws, h, "away") == "push"
    elif expect_home == "cover":
        assert grade_side(hs - aws, h, "away") == "loss"
    else:
        assert grade_side(hs - aws, h, "away") == "cover"


def test_live_book_handicap_not_negated_for_grading():
    # Odds API home point = -9.5 already means KC -9.5.
    h = to_home_handicap(-9.5, source_kind="sportsbook_handicap")
    assert h == -9.5
    assert grade_side(6.0, h, "home") == "loss"
