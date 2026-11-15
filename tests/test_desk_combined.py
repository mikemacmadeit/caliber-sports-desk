import copy
import json
import math
from datetime import timedelta

import pytest

from spe.config import settings
from spe.db.base import as_utc
from spe.models.player_td.desk_combined import (
    VERSION, AllocationHistory, combined_prediction, load_combined_model,
)
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history
from spe.shadow.atd_desk_takes import select_desk_takes, tonight_chance
from test_desk_opportunity import history_row, model, td_row

CUTOFF = as_utc("2026-09-27T18:00:00Z")


def combined_model(league="nfl"):
    return {"version": VERSION, "league": league, "player_model": model(),
            "team_coefficients": {"rush": [math.log(2), 0, 0, 0, 0, 0], "rec": [0.] * 6},
            "allocation_rates": {"rush": [.03, .2, 0], "rec": [.05, .15, .1]},
            "residual_share": {"rush": .1, "rec": .2}, "allocation_weight": .3,
            "calibration_intercept": 0., "calibration_slope": 1., "live_enabled": True,
            "artifact_sha256": "fixture"}


def records(league="nfl"):
    return [history_row(player=p, week=w, targets=4, receptions=3, rz_targets=1,
                        provider="nflverse" if league == "nfl" else "cfbd")
            for p in ("p", "unpriced") for w in (1, 2, 3)]


def allocator(rows=None):
    return AllocationHistory(OpportunityHistory(prepare_history(rows or records())))


def allocate(engine=None, m=None, unavailable=()):
    return (engine or allocator()).allocate(m or combined_model(), "home", "away", CUTOFF, True, unavailable)


@pytest.mark.parametrize("league", ["nfl", "fbs"])
def test_team_mass_is_conserved_and_unpriced_players_compete(league):
    a = allocate(allocator(records(league)), combined_model(league))
    assert set(a["players"]) == {"p", "unpriced"}
    for kind in ("rush", "rec"):
        assigned = sum(p[kind + "_lambda"] for p in a["players"].values())
        assert assigned + a["unassigned_expected_tds"][kind] == pytest.approx(a["team_expected_tds"][kind])
    assert a["players"]["p"]["probability"] == a["players"]["unpriced"]["probability"]


def test_inactive_teammate_is_removed_and_remaining_share_increases():
    normal = allocate()
    changed = allocate(unavailable={"unpriced"})
    assert "unpriced" not in changed["players"]
    assert changed["players"]["p"]["rush_lambda"] == pytest.approx(2 * normal["players"]["p"]["rush_lambda"])
    empty = allocate(unavailable={"unpriced", "p"})
    assert empty["unassigned_expected_tds"] == empty["team_expected_tds"]


def test_future_game_and_passing_touchdowns_do_not_affect_allocation():
    expected = allocate()
    rows = [dict(r, pass_td=100) for r in records()]
    rows.append(history_row(player="future", week=4, carries=1000, rush_td=50))
    assert allocate(allocator(rows)) == expected


def test_new_season_and_departed_players_do_not_inherit_a_role():
    e = allocator(records() + [history_row(player="p", week=3, game_id="new-team", team_id="away",
                                          kickoff_utc="2026-09-22T18:00:00Z")])
    assert "p" not in e.pool("home", CUTOFF)
    assert not e.pool("home", as_utc("2027-09-01T18:00:00Z"))


def test_combination_responds_to_both_components():
    e = allocator()
    f = e.history.features("p", "home", "away", "RB", CUTOFF, True)
    m = combined_model()
    original = combined_prediction(m, f, allocate(e, m), "p")
    changed = copy.deepcopy(m)
    changed["team_coefficients"]["rush"][0] += .5
    assert combined_prediction(changed, f, allocate(e, changed), "p")["probability"] > original["probability"]
    changed = copy.deepcopy(m)
    changed["player_model"]["intercept"] += .5
    assert combined_prediction(changed, f, allocate(e, changed), "p")["probability"] > original["probability"]
    assert original["probability"] == original["combined_probability"]


def test_live_prediction_selection_and_ledger_use_identical_combination(db, monkeypatch):
    # This fixture tests combination/ledger identity; policy evidence is tested separately.
    monkeypatch.setattr("spe.models.player_td.selection_safety.assess", lambda *a, **k:
        {"eligible":True,"estimated_edge":.1,"policy_version":"fixture","blockers":[]})
    from spe.models.player_td import desk_live
    from spe.shadow.desk_ledger import freeze_desk_rows, latest_decisions
    from spe.db.models import ShadowPrediction
    from test_publication_pipeline import seed
    seed(db)
    monkeypatch.setattr(desk_live, "load_model", lambda *a, **k: model())
    monkeypatch.setattr(desk_live, "load_combined_model", lambda *a, **k: combined_model())
    monkeypatch.setattr(desk_live, "load_history", lambda *a: records())
    monkeypatch.setattr("spe.models.player_td.desk_market.context_for_game", lambda *a, **k: {"usable": True, "home_implied_points":28, "away_implied_points":28})
    r = td_row()
    r.update(kickoff_utc=(CUTOFF + timedelta(days=1)).isoformat(), team_spread=28, book_price=500)
    desk_live.enrich_desk_rows(db, [r], CUTOFF)
    chance = r["forecast"]["p_estimate"]
    assert r["desk_analysis"]["combination_status"] == "active"
    assert r["desk_analysis"]["combination"]["pool_size"] == 2
    assert chance == r["desk_analysis"]["combination"]["combined_probability"]
    assert tonight_chance(r) == chance  # no hidden second spread penalty
    assert r["desk_selected"]
    freeze_desk_rows(db, [r], CUTOFF)
    saved = latest_decisions(db)[r["game_id"]].payload
    assert saved["row"]["desk_analysis"]["combination"] == r["desk_analysis"]["combination"]
    assert saved["combined_comparison"][0]["probability"] == chance
    assert db.get(ShadowPrediction, saved["shadow_id"]).forecast_probability == chance
    # Adding a second book cannot inflate the player share.
    duplicate = copy.deepcopy(r)
    duplicate["outcome_id"] = "another-book"
    desk_live.enrich_desk_rows(db, [r, duplicate], CUTOFF)
    assert r["forecast"]["p_estimate"] == duplicate["forecast"]["p_estimate"] == chance
    assert not r["forecast"]["play_enabled"]
    r["price_freshness"]["is_current_opportunity"] = False
    assert not select_desk_takes([r])


def test_combined_selection_uses_probability_not_old_handweighted_role():
    rows = [td_row("a", .4), td_row("b", .7)]
    for r in rows:
        r["book_price"] = 500
        r["market_context"], r["team_implied_points"] = {"usable": True}, 28
        r["desk_analysis"] = {"eligible": True, "combination_status": "active", "combination": {"rush_lambda": .6, "rec_lambda": 0}}
    rows[0]["usage"]["gl_carries"] = 10
    assert select_desk_takes(rows)[0]["player_id"] == "b"
    rows[1]["desk_analysis"]["eligible"] = False
    assert select_desk_takes(rows)[0]["player_id"] == "a"


def test_invalid_custom_artifact_fails_closed_and_signature_tracks_weights(db):
    from spe.publishing.local_board import scoring_signature
    path = settings.parquet_dir / "models" / f"nfl_{VERSION}.json"
    path.parent.mkdir(parents=True)
    m = combined_model()
    path.write_text(json.dumps(m))
    assert load_combined_model("nfl")
    first = scoring_signature()
    m["allocation_weight"] = .4
    path.write_text(json.dumps(m))
    assert scoring_signature() != first
    m["allocation_rates"]["rush"][0] = float("nan")
    path.write_text(json.dumps(m))
    assert load_combined_model("nfl") is None


def test_calls_endpoint_respects_latest_decision_to_pass(db):
    from app.queries import _desk_rows
    from spe.shadow.atd_desk_takes import is_desk_take
    from spe.shadow.desk_ledger import freeze_desk_rows
    from test_publication_pipeline import seed, NOW
    game = seed(db)
    r = td_row()
    freeze_desk_rows(db, [r], NOW)
    assert len(_desk_rows(db, [game], is_desk_take)) == 1
    r["desk_analysis"] = {"eligible": False}
    freeze_desk_rows(db, [r], NOW + timedelta(minutes=1))
    assert not _desk_rows(db, [game], is_desk_take)
