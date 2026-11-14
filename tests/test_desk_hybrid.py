import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from spe.db.base import as_utc
from spe.models.player_td import desk_hybrid as hybrid
from spe.models.player_td.desk_opportunity import OpportunityHistory, prepare_history
from spe.shadow.desk_hybrid_report import comparison_report
from spe.shadow.desk_ledger import capture_hybrid_comparisons, freeze_desk_rows, latest_decisions
from test_desk_opportunity import history_row, td_row
from test_publication_pipeline import NOW, seed


def synthetic_records():
    rng = np.random.default_rng(731)
    rows = []
    for year in range(2023, 2027):
        for week in range(16):
            for t in range(4):
                for p in range(8):
                    carries, targets = (16 - p*3, 2) if p < 4 else (0, 12 - p)
                    rows.append({"game_id": f"{year}-{week}-{t//2}", "player_id": f"{t}-{p}",
                        "team_id": str(t), "home_team_id": str(t//2*2), "away_team_id": str(t//2*2+1),
                        "kickoff_utc": datetime(year, 9, 1, 18, tzinfo=UTC) + timedelta(days=week*7),
                        "position": "RB" if p < 4 else "WR", "provider": "nflverse", "carries": carries,
                        "targets": targets, "receptions": targets*.7, "gl_carries": min(carries, 2),
                        "rz_targets": targets*.2, "rush_td": int(rng.random() < carries*.025),
                        "rec_td": int(rng.random() < targets*.035), "player_name": f"Player {t}-{p}"})
    return rows


BOUNDS = dict(train_end="2024-07-01", selection_end="2025-07-01",
              calibration_end="2026-07-01", test_end="2027-01-01")


@pytest.fixture(scope="module")
def fitted():
    records = synthetic_records()
    model, report = hybrid.train_hybrid(records, "nfl", **BOUNDS)
    return records, model, report


def test_future_evaluation_labels_cannot_change_model(fitted):
    records, model, report = fitted
    changed = copy.deepcopy(records)
    for row in changed:
        if row["kickoff_utc"].year == 2026:
            row["rush_td"] = 1 - row["rush_td"]
            row["rec_td"] = 1 - row["rec_td"]
    other, evaluated = hybrid.train_hybrid(changed, "nfl", **BOUNDS)
    assert model == other
    assert report["hybrid"] != evaluated["hybrid"]
    assert report["evaluation_used_for_selection"] is False
    assert not model["production_approved"]


def test_allocation_budget_and_unpriced_competition(fitted):
    records, model, _ = fitted
    history = OpportunityHistory(prepare_history(records))
    cutoff = datetime(2026, 10, 15, tzinfo=UTC)
    context = hybrid.team_context(history, "0", "1", cutoff, True)
    pool = hybrid.player_pool(history, "0", "1", cutoff, True)
    allocation, totals, other = hybrid.allocate(model, context, pool)
    assert len(pool) == 8  # independent of how many sportsbooks quote
    for kind in ("rush", "rec"):
        assert sum(v[f"lambda_{kind}"] for v in allocation.values()) + other[kind] == pytest.approx(totals[kind])
    reduced = hybrid.player_pool(history, "0", "1", cutoff, True, statuses={"0-0": "out"})
    assert "0-0" not in reduced
    active, _, _ = hybrid.allocate(model, context, reduced)
    assert active["0-1"]["lambda_rush"] > allocation["0-1"]["lambda_rush"]
    empty, _, leftover = hybrid.allocate(model, context, {})
    assert not empty and leftover == totals


def test_high_value_work_is_not_double_counted_or_invented_for_college():
    f = {"carries": 10, "gl_carries": 2, "targets": 8, "receptions": 5,
         "rz_targets": 3, "charting_known": 1}
    np.testing.assert_equal(hybrid.opportunity_vector(f, "rush", "nfl"), [8, 2])
    np.testing.assert_equal(hybrid.opportunity_vector(f, "rec", "nfl"), [5, 3])
    np.testing.assert_equal(hybrid.opportunity_vector(f, "rec", "fbs"), [5, 0])
    np.testing.assert_equal(hybrid.opportunity_vector(f, "rush", "fbs"), [10, 0])


def test_unseen_player_and_future_work_do_not_enter_pregame_pool():
    rows = [history_row(week=w) for w in (1, 2, 3)]
    rows += [history_row("new", week=4, carries=200)]
    history = OpportunityHistory(prepare_history(rows))
    pool = hybrid.player_pool(history, "home", "away", as_utc("2026-09-28T18:00:00Z"), True)
    assert set(pool) == {"p"}
    assert pool["p"]["features"]["carries"] == 8


def test_artifact_timestamp_hash_and_missing_data_fail_closed(fitted, tmp_path, monkeypatch):
    _, model, _ = fitted
    path = tmp_path / "model.json"
    monkeypatch.setattr(hybrid, "artifact_path", lambda _: path)
    assert hybrid.load_hybrid("nfl", as_utc("2026-10-01")) is None
    path.write_text(json.dumps(model))
    assert hybrid.load_hybrid("nfl", as_utc("2025-10-01")) is None
    if model["shadow_ready"]:
        assert hybrid.load_hybrid("nfl", as_utc("2026-10-01"))["artifact_sha256"] == model["artifact_sha256"]
    tampered = {**model, "team_weight": .987}
    path.write_text(json.dumps(tampered))
    assert hybrid.load_hybrid("nfl", as_utc("2026-10-01")) is None


def test_live_hybrid_uses_unpriced_roster_and_preserves_incumbent(db, fitted, monkeypatch):
    from spe.db.models import Game
    from spe.identity.roster import EligiblePlayer
    from spe.models.player_td import desk_live
    from spe.models.player_td.desk_opportunity import predict
    from test_desk_opportunity import model as incumbent
    seed(db)
    _, challenger, _ = fitted
    moment = as_utc("2026-09-27T18:00:00Z")
    db.get(Game, "g").kickoff_utc = moment + timedelta(days=1)
    records = [history_row(pid, week=w, team_id="a", home_team_id="a", away_team_id="b")
               for pid in ("priced", "unpriced") for w in (1, 2, 3)]
    monkeypatch.setattr(desk_live, "load_model", lambda league: incumbent())
    monkeypatch.setattr(desk_live, "load_history", lambda *a: records)
    monkeypatch.setattr(hybrid, "load_hybrid", lambda *a: challenger)
    monkeypatch.setattr("spe.identity.roster.eligible_players_for_game", lambda *a, **kw: [
        EligiblePlayer(pid, "a", pid, "RB", "ACT", "fixture") for pid in ("priced", "unpriced")])
    row = td_row("priced")
    row.update(team_id="a", opponent_id="b", home_abbr="A", kickoff_utc=(moment+timedelta(days=1)).isoformat())
    row["price_freshness"]["observed_at"] = moment.isoformat()
    desk_live.enrich_desk_rows(db, [row], now=moment)
    result = row["desk_analysis"]["hybrid"]
    assert result["pool_player_ids"] == ["priced", "unpriced"]
    assert result["pool_snapshot"]["unpriced"]["features"]["carries"] == 8
    assert 0 <= result["probability"] <= 1
    assert row["forecast"]["p_estimate"] == predict(incumbent(), row["desk_analysis"]["features"])
    assert row["forecast"]["base_probability"] == .6
    assert not result["play_enabled"]


def comparison_row():
    r = td_row()
    r["kickoff_utc"] = (NOW + timedelta(hours=2)).isoformat()
    r["desk_analysis"] = {"eligible": True, "features": {"carries": 8}, "blockers": [],
        "hybrid": {"probability": .55, "artifact_sha256": "a"*64, "status": "research_comparison_only"}}
    return r


def test_comparison_is_paired_append_only_and_never_becomes_official_pick(db):
    from spe.db.models import ShadowPrediction
    seed(db)
    r = comparison_row()
    assert capture_hybrid_comparisons(db, [r], NOW) == 1
    db.flush()
    assert capture_hybrid_comparisons(db, [r], NOW + timedelta(minutes=1)) == 0
    assert capture_hybrid_comparisons(db, [r], NOW + timedelta(minutes=20)) == 0  # stale price
    pred = db.query(ShadowPrediction).one()
    assert pred.forecast_probability == .55
    assert pred.components["paired_snapshot"]["incumbent_probability"] == .6
    assert as_utc(pred.forecast_as_of) == NOW
    assert not pred.play_enabled and pred.recommendation_status == "no_bet"
    assert not latest_decisions(db)
    assert r["forecast"]["p_estimate"] == .6


def test_pass_reasons_and_refreshed_quote_are_preserved(db):
    seed(db)
    row = td_row()
    assert freeze_desk_rows(db, [row], NOW) == 1
    row["price_freshness"]["observed_at"] = (NOW + timedelta(minutes=1)).isoformat()
    assert freeze_desk_rows(db, [row], NOW + timedelta(minutes=1)) == 1
    row["forecast"]["availability_status"] = "questionable"
    assert freeze_desk_rows(db, [row], NOW + timedelta(minutes=2)) == 1
    decision = latest_decisions(db)["g"].payload
    assert decision["row"] is None
    assert "availability_requires_review" in decision["pass_reasons"][0]["blockers"]


def test_report_uses_one_paired_snapshot_per_player_before_common_cutoff():
    def pred(sid, minutes, p):
        return SimpleNamespace(shadow_id=sid, game_id="g", player_id="p", league_id="nfl",
            model_version_id="desk_hybrid_v1-a", forecast_as_of=NOW+timedelta(minutes=minutes),
            forecast_probability=p, price_american=150, components={"paired_snapshot": {
                "kickoff_utc": (NOW+timedelta(hours=2)).isoformat(), "incumbent_probability": .4,
                "incumbent_version": "desk_opportunity_v2-a", "position": "RB", "common_blockers": []}})
    predictions = [pred("old", 0, .3), pred("latest", 30, .6), pred("too_late", 90, .9)]
    settlements = {p.shadow_id: SimpleNamespace(result="win", flat_stake_return=1.5) for p in predictions}
    result = comparison_report(predictions, settlements)
    report = result["comparisons"][0]
    assert report["hybrid"]["n"] == 1
    assert report["hybrid"]["brier"] == pytest.approx(.16)
    assert result["snapshots_after_cutoff"] == 1
    assert report["brier_difference_game_bootstrap_95"] is None
