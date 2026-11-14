from datetime import timedelta

import pytest

from spe.db.base import as_utc
from spe.models.player_td.desk_opportunity import FEATURES, OpportunityHistory, prepare_history, predict
from spe.models.player_td.desk_live import enrich_desk_rows, load_history
from spe.publishing.policy import assess
from spe.shadow.atd_desk_takes import select_desk_takes
from spe.shadow.desk_ledger import latest_decisions, freeze_desk_rows
from test_publication_pipeline import NOW, seed, row as published_row


def history_row(player="p", week=1, **values):
    return {"game_id": f"g{week}", "player_id": player, "team_id": "home",
        "home_team_id": "home", "away_team_id": "away", "kickoff_utc": f"2026-09-{week * 7:02d}T18:00:00Z",
        "position": "RB", "provider": "nflverse", "carries": 8, "targets": 0,
        "receptions": 0, "rush_td": 0, "rec_td": 0, "gl_carries": 2, "rz_targets": 0,
        "player_name": player, **values}


def model():
    coefficients = [0.] * len(FEATURES)
    coefficients[FEATURES.index("gl_carries")] = .7
    coefficients[FEATURES.index("carries")] = .02
    return {"features": FEATURES, "center": [0.] * len(FEATURES), "scale": [1.] * len(FEATURES),
            "coefficients": coefficients, "intercept": -2., "research_approved": True}


def test_goal_line_back_can_outrank_volume_back_without_recent_touchdowns():
    high_value = {"carries": 6, "gl_carries": 3}
    volume = {"carries": 22, "gl_carries": 0}
    assert predict(model(), high_value) > predict(model(), volume)


def test_no_future_target_game_or_previous_team_leaks_into_workload():
    records = [history_row(week=i) for i in (1, 2, 3)]
    records += [history_row(week=4, carries=1000, rush_td=9),
                history_row(week=2, game_id="other-club", team_id="away", home_team_id="other", carries=900)]
    h = OpportunityHistory(prepare_history(records))
    f = h.features("p", "home", "away", "RB", as_utc("2026-09-28T18:00:00Z"), True)
    assert f["carries"] == 8
    assert f["rush_td"] == 0
    assert f["games"] == 3


def test_college_placeholder_zeros_are_missing_and_passing_tds_are_not_labels():
    rows = prepare_history([history_row(provider="cfbd", pass_td=5)])
    assert rows[0]["targets"] is None
    assert rows[0]["gl_carries"] is None
    assert rows[0]["rz_targets"] is None
    assert rows[0]["rush_td"] + rows[0]["rec_td"] == 0


def test_postseason_retains_season_but_new_season_does_not_inherit_workload():
    records = prepare_history([history_row(week=i) for i in (1, 2, 3)])
    h = OpportunityHistory(records)
    assert h.features("p", "home", "away", "RB", as_utc("2027-01-01T12:00:00Z"), True)
    assert h.features("p", "home", "away", "RB", as_utc("2027-09-01T12:00:00Z"), True) is None


def test_duplicate_provider_games_count_as_one_appearance():
    a = history_row(week=1)
    b = history_row(week=1, game_id="alternate-game-id", provider="espn.box")
    assert len(prepare_history([a, b])) == 1
    assert prepare_history([a, b])[0]["provider"] == "nflverse"


def td_row(name="p", chance=.6):
    r = published_row(market_id="anytime_td", side="yes", line=None, player_id=name,
        player_name=name, outcome_id=name, team_id="home", opponent_id="away", position="RB",
        book_price=500,
        forecast={"p_estimate": chance, "availability_status": "active"})
    r["usage"] = {"carries": 8, "gl_carries": 2}
    return r


@pytest.mark.parametrize("status", ["questionable", "inactive", "confirmed_inactive", "out", "doubtful"])
def test_uncertain_or_inactive_player_never_selected(status):
    bad = td_row("star", .9)
    bad["forecast"]["availability_status"] = status
    assert select_desk_takes([bad, td_row("ready", .3)], now=NOW)[0]["player_id"] == "ready"


@pytest.mark.parametrize("p", [float("nan"), float("inf"), 1.2, -.1, .21])
def test_invalid_or_too_weak_probability_never_selected(p):
    assert not select_desk_takes([td_row(chance=p)], now=NOW)


def test_equal_probabilities_do_not_depend_on_row_order():
    rows = [td_row("a"), td_row("b")]
    assert select_desk_takes(rows, now=NOW)[0]["player_id"] == select_desk_takes(rows[::-1], now=NOW)[0]["player_id"]


def test_live_model_preserves_base_and_never_enables_value_claims(db, monkeypatch):
    import spe.models.player_td.desk_live as live
    monkeypatch.setattr(live, "load_model", lambda league: model())
    monkeypatch.setattr(live, "load_history", lambda *args: [history_row(week=i) for i in (1, 2, 3)])
    r = td_row()
    r["kickoff_utc"] = "2026-09-28T18:00:00Z"
    r["league_id"] = "nfl"
    r["price_freshness"]["observed_at"] = "2026-09-27T18:00:00Z"
    enrich_desk_rows(db, [r], now=as_utc("2026-09-27T18:00:00Z"))
    assert r["desk_selected"]
    assert r["forecast"]["base_probability"] == .6
    assert r["forecast"]["ev_after_haircut"] is None
    assert "DESK_EXPERIMENTAL" in r["forecast"]["reason_codes"]
    assert not assess(r, as_utc("2026-09-27T18:00:00Z"))["eligible"]


def test_database_loader_handles_empty_history(db):
    seed(db)
    assert load_history(db, "nfl", NOW) == []


def test_charting_is_player_specific_and_survives_a_box_score_refresh(db):
    from spe.db.models import Game, Player, PlayerGameStat
    seed(db)
    game = db.get(Game, "g")
    game.status, game.data_tier = "final", "historical"
    game.kickoff_utc = NOW - timedelta(days=7)
    for pid in ("charted", "unknown"):
        db.add(Player(player_id=pid, name=pid, position_group="RB", position="RB"))
    db.flush()
    provenance = dict(provider="nflverse", provider_record_id="test", observed_at=NOW,
                      effective_at=game.kickoff_utc, ingested_at=NOW)
    db.add(PlayerGameStat(game_id="g", player_id="charted", team_id="a", revision=1,
        carries=8, gl_carries=3, rz_targets=1, quality_status="pbp_opportunities", **provenance))
    db.add(PlayerGameStat(game_id="g", player_id="charted", team_id="a", revision=2,
        carries=12, quality_status="ok", **provenance))
    db.add(PlayerGameStat(game_id="g", player_id="unknown", team_id="a", revision=1,
        carries=5, quality_status="ok", **provenance))
    db.flush()
    rows = {r["player_id"]: r for r in load_history(db, "nfl", NOW)}
    assert rows["charted"]["carries"] == 12
    assert rows["charted"]["gl_carries"] == 3
    assert rows["unknown"]["gl_carries"] is None
    assert rows["unknown"]["rz_targets"] is None


def test_pregame_choice_is_frozen_and_not_replaced_after_kickoff(db):
    from spe.db.models import DeskDecision, ShadowPrediction
    seed(db)
    first = td_row("first")
    first["kickoff_utc"] = (NOW + timedelta(hours=1)).isoformat()
    first["forecast"]["model_version"] = "desk_opportunity_v2-nfl"
    assert freeze_desk_rows(db, [first], NOW) == 1
    assert freeze_desk_rows(db, [first], NOW + timedelta(minutes=1)) == 0
    old_id = latest_decisions(db)[first["game_id"]].payload["shadow_id"]
    second = {**first, "player_id": "second", "player_name": "second", "outcome_id": "second"}
    assert freeze_desk_rows(db, [second], NOW + timedelta(minutes=2)) == 1
    assert db.get(ShadowPrediction, old_id).player_id == "first"
    assert freeze_desk_rows(db, [first], NOW + timedelta(hours=2)) == 0
    saved = latest_decisions(db)[first["game_id"]].payload
    assert saved["row"]["player_id"] == "second"
    assert db.query(ShadowPrediction).count() == 2
    assert db.query(DeskDecision).count() == 2


def test_review_flag_records_a_pass_and_does_not_keep_previous_pick(db):
    seed(db)
    r = td_row()
    r["kickoff_utc"] = (NOW + timedelta(hours=1)).isoformat()
    freeze_desk_rows(db, [r], NOW)
    r["forecast"]["availability_status"] = "questionable"
    freeze_desk_rows(db, [r], NOW + timedelta(minutes=1))
    assert latest_decisions(db)[r["game_id"]].payload["row"] is None


@pytest.mark.parametrize("action", ["rewrite", "delete"])
def test_frozen_decision_rejects_mutation(db, action):
    seed(db)
    freeze_desk_rows(db, [td_row()], NOW)
    db.commit()
    record = latest_decisions(db)["g"]
    if action == "rewrite":
        record.payload = {"row": None}
    else:
        db.delete(record)
    with pytest.raises(ValueError, match="append-only"):
        db.flush()
    db.rollback()


def test_expired_quote_records_a_pass_instead_of_claiming_old_value(db):
    seed(db)
    r = td_row()
    freeze_desk_rows(db, [r], NOW)
    assert latest_decisions(db)["g"].payload["row"]
    freeze_desk_rows(db, [r], NOW + timedelta(minutes=20))
    assert latest_decisions(db)["g"].payload["row"] is None


def test_scorecard_counts_only_final_pregame_choice_and_omits_stale_price_roi():
    from types import SimpleNamespace
    from spe.shadow.report import _atd_scorecard
    def pred(sid):
        return SimpleNamespace(shadow_id=sid, game_id="g", market_id="anytime_td", league_id="nfl",
            model_version_id="desk_opportunity_v2-nfl", forecast_probability=.6, player_id=sid,
            price_american=100, components={"desk_take":"game_scorer", "desk_take_source":"desk_opportunity_v2",
                                            "quote_verified":False})
    old, latest = pred("old"), pred("latest")
    settlements = {"old":SimpleNamespace(result="win",flat_stake_return=1),
                   "latest":SimpleNamespace(result="loss",flat_stake_return=-1)}
    result = _atd_scorecard([old, latest], settlements, {"latest"}, {"g"})
    assert result["rows"][0]["n"] == 1
    assert result["rows"][0]["wins"] == 0
    assert result["rows"][0]["flat_stake_roi"] is None
    assert _atd_scorecard([old, latest], settlements, set(), {"g"})["rows"] == []
