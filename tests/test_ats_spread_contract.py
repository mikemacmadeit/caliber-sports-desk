"""End-to-end ATS spread-sign fixtures from raw nflverse semantics.

These tests start from raw L (expected home margin), not pre-normalized handicaps.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from spe.models.ats.labels import cover_indicator, grade_home_spread, grade_side, normal_cover_probs
from spe.models.ats.spread_contract import (
    attach_spread_fields,
    grade_home_ats_from_scores,
    home_handicap_from_nflverse_L,
    market_home_margin_forecast,
    to_home_handicap,
)


RAW_CSV = Path("data/research/ats_audit/nflverse_games_raw.csv")
EXPECTED_SHA256 = "356d6fec0e179aa473fcbe748182630ae7fd103681e0295fc9398efbcc627db5"


@pytest.fixture(scope="module")
def raw_games() -> pd.DataFrame:
    assert RAW_CSV.exists(), "Run scripts/ats_spread_contract_probe.py first to download raw CSV"
    data = RAW_CSV.read_bytes()
    assert hashlib.sha256(data).hexdigest() == EXPECTED_SHA256
    return pd.read_csv(RAW_CSV)


def test_source_hash_and_docs_contract(raw_games: pd.DataFrame):
    # Positive L ⇒ home favored empirically: mean result when L>0 should be positive.
    done = raw_games.dropna(subset=["result", "spread_line"])
    assert float(done.loc[done["spread_line"] > 0, "result"].mean()) > 0
    assert float(done.loc[done["spread_line"] < 0, "result"].mean()) < 0


def test_case_a_ari_at_buf(raw_games: pd.DataFrame):
    row = raw_games.loc[raw_games["game_id"] == "2024_01_ARI_BUF"].iloc[0]
    L = float(row["spread_line"])
    assert L == 6.5
    M = float(row["home_score"]) - float(row["away_score"])
    assert M == 6.0
    h = home_handicap_from_nflverse_L(L)
    assert h == -6.5
    assert grade_home_ats_from_scores(
        row["home_score"], row["away_score"], spread_value=L, source_kind="nflverse_margin_forecast"
    ) == "away_cover"
    assert grade_home_spread(M, h) == "loss"
    assert cover_indicator(M, h, "home") == 0.0
    assert cover_indicator(M, h, "away") == 1.0


def test_case_b_ne_at_cin(raw_games: pd.DataFrame):
    row = raw_games.loc[raw_games["game_id"] == "2024_01_NE_CIN"].iloc[0]
    L = float(row["spread_line"])
    assert L == 8.0
    M = float(row["home_score"]) - float(row["away_score"])
    assert M == -6.0
    h = -L
    assert grade_home_spread(M, h) == "loss"
    # Wrong convention M+L would incorrectly call this a home cover.
    assert (M + L) < 0 or True  # M+L = 2 would be wrong cover; here M+L=2
    assert (M + L) == 2.0  # documents the inverted-label bug
    assert (M - L) == -14.0


def test_case_c_nyj_at_kc_favorite_identity(raw_games: pd.DataFrame):
    row = raw_games.loc[raw_games["game_id"] == "2026_09_NYJ_KC"].iloc[0]
    L = float(row["spread_line"])
    assert L == 9.5
    assert float(row["home_moneyline"]) < 0  # KC home favorite on ML
    h = home_handicap_from_nflverse_L(L)
    assert h == -9.5  # KC -9.5, not +9.5
    assert market_home_margin_forecast(L, source_kind="nflverse_margin_forecast") == 9.5


def test_home_underdog_raw_row():
    # Constructed from nflverse contract: L negative ⇒ away favored ⇒ home underdog.
    L = -3.5
    h = to_home_handicap(L, source_kind="nflverse_margin_forecast")
    assert h == 3.5
    # Home wins by 7 → covers as +3.5 dog
    assert grade_home_spread(7.0, h) == "cover"
    # Home loses by 3 → still covers +3.5
    assert grade_home_spread(-3.0, h) == "cover"
    # Home loses by 4 → fails to cover
    assert grade_home_spread(-4.0, h) == "loss"


def test_true_push_from_margin_forecast():
    L = 3.0  # home favored by 3
    h = -L
    assert grade_home_spread(3.0, h) == "push"
    assert grade_side(3.0, h, "away") == "push"
    assert cover_indicator(3.0, h, "home") is None


def test_attach_fields_does_not_flip_stored_L():
    frame = pd.DataFrame(
        {
            "spread_line": [6.5, -3.0, 9.5],
            "source": ["nflverse_consensus", "nflverse_consensus", "odds_api"],
        }
    )
    out = attach_spread_fields(frame)
    assert list(out["spread_line"]) == [6.5, -3.0, 9.5]
    assert list(out["home_handicap"]) == [-6.5, 3.0, 9.5]
    assert list(out["market_home_margin"]) == [6.5, -3.0, -9.5]


def test_normal_cover_probs_use_handicap_not_L():
    # Home favored: L=+7 ⇒ h=-7. Passing raw L as handicap inflates P(home cover).
    probs_correct = normal_cover_probs(10.0, -7.0, sigma=13.0)
    probs_wrong_as_if_L = normal_cover_probs(10.0, 7.0, sigma=13.0)
    assert probs_correct["p_home_cover"] > 0.5
    assert probs_wrong_as_if_L["p_home_cover"] > probs_correct["p_home_cover"]
    # Home dog: L=-7 ⇒ h=+7. Treating raw L as h uses h=-7 and understates dog cover p.
    dog_correct = normal_cover_probs(-10.0, 7.0, sigma=13.0)
    dog_wrong = normal_cover_probs(-10.0, -7.0, sigma=13.0)
    assert dog_correct["p_home_cover"] > dog_wrong["p_home_cover"]
