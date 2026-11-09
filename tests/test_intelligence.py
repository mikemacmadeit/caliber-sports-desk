import json
from datetime import UTC, datetime, timedelta

from spe.db.models import (
    AiExplanation,
    AiFinding,
    AiOpportunity,
    AiReview,
    AiRun,
    AiSignal,
    BacktestPrediction,
    BacktestRun,
    Forecast,
    Game,
    InjuryEvent,
    League,
    Market,
    ModelVersion,
    Outcome,
    Player,
    PlayerAnalytics,
    Recommendation,
    Season,
    Sport,
    Team,
    TeamAnalytics,
    Venue,
)
from spe.intelligence.client import Usage
from spe.intelligence.context import build_research_context
from spe.intelligence.research import research_board, research_game
from spe.intelligence.review import classify_miss, review_backtests
from spe.intelligence.schema import (
    Evidence,
    ExplanationOutput,
    Finding,
    ForecastEcho,
    Opportunity,
    ResearchOutput,
    ReviewOutput,
    Signal,
)
from spe.intelligence.scenarios import plan_from_signals
from spe.intelligence.validate import (
    misstates_direction,
    repair_research_evidence,
    validate_explanation,
    validate_research,
)


NOW = datetime(2026, 11, 6, 18, tzinfo=UTC)


class Scripted:
    model = "test"

    def __init__(self, research: ResearchOutput, explanation: ExplanationOutput | None = None, bad_status: str | None = None):
        self.research = research
        self.explanation = explanation
        self.bad_status = bad_status
        self.calls = 0

    def complete(self, *, instructions, user, schema):
        self.calls += 1
        payload = json.loads(user)
        if schema is ExplanationOutput:
            if self.bad_status:
                rows = [
                    ForecastEcho(
                        forecast_id=row["forecast_id"],
                        recommendation_status=self.bad_status,
                        reason_codes=row["reason_codes"],
                    )
                    for row in payload["forecasts"]
                ]
                return ExplanationOutput(narrative="Play it.", forecasts=rows), Usage(1, 1)
            rows = [
                ForecastEcho(
                    forecast_id=row["forecast_id"],
                    recommendation_status=row["recommendation_status"],
                    reason_codes=row["reason_codes"],
                )
                for row in payload["forecasts"]
            ]
            return ExplanationOutput(narrative="The Python call stands.", factors=["price"], forecasts=rows), Usage(1, 1)
        if schema is ReviewOutput:
            return ReviewOutput(flags=[], abstain_reason="No suspicious forecasts."), Usage(1, 1)
        return self.research, Usage(4, 5)


def _game(db) -> Game:
    db.add(Sport(sport_id="football", name="American football"))
    db.flush()
    db.add(League(league_id="nfl", sport_id="football", name="NFL", classification="nfl"))
    db.add(
        Season(
            season_id="nfl-2026",
            league_id="nfl",
            year=2026,
            start_at=datetime(2026, 9, 1, tzinfo=UTC),
            end_at=datetime(2027, 2, 15, tzinfo=UTC),
        )
    )
    db.add(Venue(venue_id="v1", name="Test Bowl", lat=1, lon=1, roof_type="open", surface="grass"))
    db.add_all(
        [
            Team(team_id="nfl:AAA", league_id="nfl", name="A", abbreviation="AAA"),
            Team(team_id="nfl:BBB", league_id="nfl", name="B", abbreviation="BBB"),
        ]
    )
    db.add(
        Player(player_id="p1", name="Runner", position_group="RB", position="RB")
    )
    db.flush()
    game = Game(
        game_id="g1",
        season_id="nfl-2026",
        league_id="nfl",
        home_team_id="nfl:AAA",
        away_team_id="nfl:BBB",
        kickoff_utc=NOW + timedelta(days=2),
        venue_id="v1",
        status="scheduled",
        week=5,
        data_tier="demo",
    )
    db.add(game)
    db.add(
        Market(
            market_id="anytime_td",
            market_type="player",
            stat="touchdown",
            grading_rule="anytime",
        )
    )
    db.flush()
    return game


def _injury(db, when: datetime, event_id: str = "inj1") -> None:
    db.add(
        InjuryEvent(
            injury_event_id=event_id,
            game_id="g1",
            player_id="p1",
            designation="Out",
            reported_at=when,
            source="nflverse",
            confidence=0.9,
            coverage_complete=True,
            provider="nflverse",
            provider_record_id=event_id,
            observed_at=when,
            effective_at=when,
        )
    )
    db.flush()


def test_repairs_swapped_evidence_citations():
    output = ResearchOutput(
        findings=[
            Finding(
                topic="injury",
                claim_type="fact",
                statement="Runner is out.",
                confidence=0.9,
                affected_player_ids=["p1"],
                evidence=[Evidence(source="inj1", ref="inj1")],
            )
        ]
    )
    repaired = repair_research_evidence(output, {"injury_events": {"inj1"}})
    assert repaired.findings[0].evidence[0].source == "injury_events"
    assert repaired.findings[0].evidence[0].ref == "inj1"
    assert not validate_research(repaired, {"p1"}, {"injury_events": {"inj1"}})


def test_rejects_unknown_player_and_unsourced_fact():
    allowed = {"p1"}
    evidence = {"injury_events": {"inj1"}}
    ghost = ResearchOutput(
        findings=[
            Finding(
                topic="injury",
                claim_type="fact",
                statement="A made-up player is out.",
                confidence=0.9,
                affected_player_ids=["ghost"],
                evidence=[Evidence(source="injury_events", ref="inj1")],
            )
        ]
    )
    assert any("unknown player" in err for err in validate_research(ghost, allowed, evidence))
    bare = ResearchOutput(
        findings=[Finding(topic="weather", claim_type="fact", statement="It is raining.", confidence=0.8)]
    )
    assert any("no source" in err for err in validate_research(bare, allowed, evidence))


def test_rejects_contradictory_facts():
    output = ResearchOutput(
        findings=[
            Finding(
                topic="injury",
                claim_type="fact",
                statement="Out.",
                confidence=0.9,
                subject_key="injury:p1",
                stance="out",
                affected_player_ids=["p1"],
                evidence=[Evidence(source="injury_events", ref="inj1")],
            ),
            Finding(
                topic="injury",
                claim_type="fact",
                statement="Healthy.",
                confidence=0.9,
                subject_key="injury:p1",
                stance="healthy",
                affected_player_ids=["p1"],
                evidence=[Evidence(source="injury_events", ref="inj1")],
            ),
        ]
    )
    errors = validate_research(output, {"p1"}, {"injury_events": {"inj1"}})
    assert any("contradicts" in err for err in errors)
    soft = ResearchOutput(
        findings=[
            Finding(
                topic="injury",
                claim_type="fact",
                statement="Limited practice.",
                confidence=0.7,
                subject_key="injury:p1",
                stance="limited practice participation",
                affected_player_ids=["p1"],
                evidence=[Evidence(source="injury_events", ref="inj1")],
            ),
            Finding(
                topic="workload",
                claim_type="fact",
                statement="Rushing heavy.",
                confidence=0.6,
                subject_key="injury:p1",
                stance="workload concentrated on rushing",
                affected_player_ids=["p1"],
                evidence=[Evidence(source="injury_events", ref="inj1")],
            ),
        ]
    )
    assert not any("contradicts" in err for err in validate_research(soft, {"p1"}, {"injury_events": {"inj1"}}))


def test_direction_words_cannot_contradict_the_numbers():
    bad = "EPA improved from week 8 (0.12) to week 9 (-0.10)"
    assert misstates_direction(bad)
    assert not misstates_direction("Rushing yards were 76.0 in week 9")


def test_explanation_cannot_rewrite_the_call():
    expected = [{"forecast_id": "f1", "recommendation_status": "no_bet", "reason_codes": ["NO_CURRENT_PRICE"]}]
    bad = ExplanationOutput(
        narrative="Play.",
        forecasts=[ForecastEcho(forecast_id="f1", recommendation_status="play", reason_codes=["NO_CURRENT_PRICE"])],
    )
    assert validate_explanation(bad, expected)


def test_context_hides_future_injury_and_same_week_workload(db):
    game = _game(db)
    _injury(db, NOW - timedelta(hours=2), "inj-early")
    _injury(db, NOW + timedelta(days=1), "inj-late")
    db.add(
        PlayerAnalytics(
            league_id="nfl",
            player_id="p1",
            team_id="nfl:AAA",
            season_year=2026,
            week=4,
            game_id="",
            grain="week",
            metrics={"targets": 6},
            coverage={},
            sample_size=1,
            source="test",
        )
    )
    db.add(
        PlayerAnalytics(
            league_id="nfl",
            player_id="p1",
            team_id="nfl:AAA",
            season_year=2026,
            week=5,
            game_id="later",
            grain="week",
            metrics={"targets": 99},
            coverage={},
            sample_size=1,
            source="test",
        )
    )
    db.add(
        TeamAnalytics(
            league_id="nfl",
            team_id="nfl:AAA",
            season_year=2026,
            week=4,
            game_id="",
            grain="week",
            metrics={"off_epa_per_play": 0.08, "pace_sec_per_play": 27.0, "points_per_scoring_opp": 2.1},
            coverage={},
            sample_size=1,
            source="test",
        )
    )
    db.add(
        TeamAnalytics(
            league_id="nfl",
            team_id="nfl:BBB",
            season_year=2026,
            week=4,
            game_id="",
            grain="week",
            metrics={"def_epa_per_play": -0.04},
            coverage={},
            sample_size=1,
            source="test",
        )
    )
    db.flush()
    packet = build_research_context(db, game, NOW)
    refs = {row["ref"] for row in packet["injuries"]}
    assert refs == {"inj-early"}
    weeks = [row["week"] for row in packet["player_workload"]]
    assert weeks == [4]
    assert "coaching_staff" in packet["gaps"]
    assert packet["league_rules"]["league_id"] == "nfl"
    assert packet["home_field"]["home_team_id"] == "nfl:AAA"
    assert packet["matchups"]["home_offense_vs_away_defense"]["offense"]["metrics"]["off_epa_per_play"] == 0.08
    assert packet["scoring_expectations"]["home"]["points_per_scoring_opp"] == 2.1
    assert packet["players"][0]["team_id"] == "nfl:AAA"
    assert "player injuries and availability" in packet["investigation_topics"]


def test_validated_signal_simulates_without_changing_official_forecast(db):
    game = _game(db)
    _injury(db, NOW - timedelta(hours=1))
    db.add(
        ModelVersion(
            model_version_id="mv1",
            code_sha="test",
            training_cutoff=NOW,
            data_snapshot="test",
            hyperparameters={},
            calibration_version="v0",
            league_id="nfl",
            family="test",
        )
    )
    db.flush()
    db.add(Outcome(outcome_id="o1", market_id="anytime_td", game_id="g1", player_id="p1", side="yes", description=""))
    db.flush()
    forecast = Forecast(
        forecast_id="f1",
        outcome_id="o1",
        as_of=NOW,
        model_version_id="mv1",
        p=0.22,
        p_low=0.1,
        p_high=0.3,
        sim_count=10,
        data_cutoff=NOW,
        availability_status="out",
        reason_codes=["PRICE_TIED"],
        components={},
        is_official=True,
        data_tier="demo",
    )
    db.add(forecast)
    db.flush()
    db.add(
        Recommendation(
            forecast_id="f1",
            threshold=0.04,
            status="no_bet",
            reason_codes=["NO_CURRENT_PRICE"],
            data_tier="demo",
        )
    )
    db.flush()
    research = ResearchOutput(
        findings=[
            Finding(
                topic="injury",
                claim_type="fact",
                statement="Runner is out.",
                confidence=0.95,
                affected_player_ids=["p1"],
                evidence=[Evidence(source="injury_events", ref="inj1")],
                subject_key="injury:p1",
                stance="out",
            )
        ],
        signals=[
            Signal(
                player_id="p1",
                metric="touchdowns",
                direction="down",
                magnitude="material",
                claim_type="fact",
                statement="Touchdown chances drop because he is out.",
                confidence=0.9,
                evidence=[Evidence(source="injury_events", ref="inj1")],
            )
        ],
    )
    client = Scripted(research)
    run = research_game(db, game, client, as_of=NOW)
    db.flush()
    assert run.status == "accepted"
    signal = db.query(AiSignal).one()
    assert signal.validation_status == "validated"
    assert signal.scenario_status == "validated_research"
    assert signal.simulation_run_id
    assert db.get(Forecast, "f1").p == 0.22
    explanation = db.query(AiExplanation).one()
    assert explanation.recommendation_status == "demo_play" or explanation.recommendation_status == "no_bet"
    assert explanation.recommendation_status == "no_bet"
    again = research_game(db, game, client, as_of=NOW)
    assert again.run_id == run.run_id
    # research + explanation + prediction review on first pass; cache on second
    assert client.calls == 3


def test_hypothetical_signal_does_not_simulate(db):
    game = _game(db)
    _injury(db, NOW - timedelta(hours=1))
    research = ResearchOutput(
        signals=[
            Signal(
                player_id="p1",
                metric="targets",
                direction="up",
                magnitude="material",
                claim_type="hypothetical",
                statement="If the role expands, targets rise.",
                confidence=0.4,
            )
        ]
    )
    run = research_game(db, game, Scripted(research), as_of=NOW)
    db.flush()
    assert run.status == "accepted"
    signal = db.query(AiSignal).one()
    assert signal.validation_status == "unverified"
    assert signal.scenario_status == "speculative"
    assert signal.simulation_run_id is None
    assert db.query(AiFinding).count() == 0


def test_scenario_plan_validates_out_designation_and_keeps_hypotheticals_research_only(db):
    game = _game(db)
    _injury(db, NOW - timedelta(hours=1))
    packet = build_research_context(db, game, NOW)
    research = ResearchOutput(
        signals=[
            Signal(
                player_id="p1",
                metric="touchdowns",
                direction="down",
                magnitude="material",
                claim_type="fact",
                statement="Out.",
                confidence=0.9,
                evidence=[Evidence(source="injury_events", ref="inj1")],
            ),
            Signal(
                player_id="p1",
                metric="targets",
                direction="up",
                magnitude="material",
                claim_type="hypothetical",
                statement="If role expands.",
                confidence=0.4,
            ),
        ]
    )
    plan = plan_from_signals(packet, research.signals)
    assert any(row.kind == "availability" and row.value == 0.0 for row in plan.validated_changes)
    assert any(row.kind == "workload_targets" for row in plan.hypothetical_changes)
    assert plan.research_only is True


def test_opportunity_requires_analysis_and_rejects_invented_edge(db):
    allowed = {"p1"}
    evidence = {"injury_events": {"inj1"}}
    bare = ResearchOutput(
        opportunities=[
            Opportunity(
                player_id="p1",
                market_id="rushing_yards",
                claim_type="inference",
                explanation="Backup may see more carries.",
                evidence_confidence=0.6,
                missing_information=["confirmed snap share"],
                recommended_analysis="",
            )
        ]
    )
    assert any("recommended_analysis" in err for err in validate_research(bare, allowed, evidence))
    edged = ResearchOutput(
        opportunities=[
            Opportunity(
                player_id="p1",
                market_id="rushing_yards",
                claim_type="inference",
                explanation="Edge of 4% versus the book.",
                evidence_confidence=0.6,
                missing_information=["Python forecast + current price"],
                recommended_analysis="Compare frozen forecast to the posted rushing line.",
            )
        ]
    )
    assert any("numeric edge" in err for err in validate_research(edged, allowed, evidence))


def test_stores_opportunity_without_numeric_edge(db):
    game = _game(db)
    _injury(db, NOW - timedelta(hours=1))
    research = ResearchOutput(
        opportunities=[
            Opportunity(
                player_id="p1",
                market_id="anytime_td",
                claim_type="fact",
                explanation="Runner is out, so teammate TD opportunity may rise.",
                evidence_confidence=0.9,
                evidence=[Evidence(source="injury_events", ref="inj1")],
                missing_information=["replacement snap share"],
                recommended_analysis="Rerun the anytime TD simulation with the starter availability at 0.",
            )
        ]
    )
    run = research_game(db, game, Scripted(research), as_of=NOW)
    db.flush()
    assert run.status == "accepted"
    row = db.query(AiOpportunity).one()
    assert row.market_id == "anytime_td"
    assert row.game_id == "g1"
    assert row.validation_status == "evidence_backed"
    assert row.recommended_analysis.startswith("Rerun")
    assert row.evidence[0]["observed_at"]
    assert "numeric_edge" not in (run.output.get("opportunities")[0] or {})


def test_research_board_keeps_leagues_separate(db):
    _game(db)
    db.add(League(league_id="fbs", sport_id="football", name="FBS", classification="fbs"))
    db.add(
        Season(
            season_id="fbs-2026",
            league_id="fbs",
            year=2026,
            start_at=datetime(2026, 8, 1, tzinfo=UTC),
            end_at=datetime(2027, 1, 15, tzinfo=UTC),
        )
    )
    db.add_all(
        [
            Team(team_id="fbs:AAA", league_id="fbs", name="College A", abbreviation="CAA"),
            Team(team_id="fbs:BBB", league_id="fbs", name="College B", abbreviation="CBB"),
        ]
    )
    db.flush()
    db.add(
        Game(
            game_id="g-fbs",
            season_id="fbs-2026",
            league_id="fbs",
            home_team_id="fbs:AAA",
            away_team_id="fbs:BBB",
            kickoff_utc=NOW + timedelta(days=1),
            venue_id="v1",
            status="scheduled",
            week=5,
            data_tier="upcoming",
        )
    )
    db.flush()
    nfl_rows = research_board(db, Scripted(ResearchOutput()), limit=5, league_id="nfl")
    fbs_rows = research_board(db, Scripted(ResearchOutput()), limit=5, league_id="fbs")
    assert all(row["league_id"] == "nfl" for row in nfl_rows)
    assert all(row["league_id"] == "fbs" for row in fbs_rows)
    assert {row["game_id"] for row in nfl_rows} == {"g1"}
    assert {row["game_id"] for row in fbs_rows} == {"g-fbs"}


def test_bad_explanation_is_rejected(db):
    game = _game(db)
    db.add(
        ModelVersion(
            model_version_id="mv1",
            code_sha="test",
            training_cutoff=NOW,
            data_snapshot="test",
            hyperparameters={},
            calibration_version="v0",
            league_id="nfl",
            family="test",
        )
    )
    db.flush()
    db.add(Outcome(outcome_id="o1", market_id="anytime_td", game_id="g1", player_id="p1", side="yes", description=""))
    db.flush()
    db.add(
        Forecast(
            forecast_id="f1",
            outcome_id="o1",
            as_of=NOW,
            model_version_id="mv1",
            p=0.22,
            p_low=0.1,
            p_high=0.3,
            sim_count=10,
            data_cutoff=NOW,
            availability_status="unknown",
            reason_codes=[],
            is_official=True,
            data_tier="demo",
        )
    )
    db.flush()
    db.add(Recommendation(forecast_id="f1", threshold=0.04, status="no_bet", reason_codes=["NO_CURRENT_PRICE"], data_tier="demo"))
    db.flush()
    client = Scripted(ResearchOutput(), bad_status="play")
    research_game(db, game, client, as_of=NOW)
    db.flush()
    assert db.query(AiExplanation).count() == 0
    rejected = db.query(AiRun).filter(AiRun.kind == "explanation").one()
    assert rejected.status == "rejected"


def test_settled_miss_uses_box_score_for_playing_time_and_never_applies(db):
    from spe.db.models import PlayerGameStat
    from spe.intelligence.improve import classify_settled_miss, review_settled_misses

    assert classify_settled_miss(p=0.7, y=0, stats={"targets": 0, "carries": 0}) == "playing_time"
    assert classify_settled_miss(p=0.7, y=0, market_id="anytime_td", stats={"targets": 10, "carries": 5}) == "opportunity"
    assert classify_settled_miss(p=0.7, y=0, inactive=True) == "injuries"
    game = _game(db)
    db.add(
        PlayerGameStat(
            game_id=game.game_id,
            player_id="p1",
            targets=0,
            carries=0,
            receptions=0,
            rush_yards=0,
            rec_yards=0,
            provider="test",
            provider_record_id="pgs1",
            observed_at=NOW,
            effective_at=NOW,
        )
    )
    db.add(
        BacktestRun(
            run_id="bt-settle",
            league_id="nfl",
            market_id="anytime_td",
            model_family="naive",
            train_end=NOW,
            test_start=NOW,
            test_end=NOW,
            n=1,
        )
    )
    db.flush()
    db.add(
        BacktestPrediction(
            id="bp-settle",
            run_id="bt-settle",
            game_id=game.game_id,
            player_id="p1",
            as_of=NOW,
            train_cutoff=NOW,
            p=0.8,
            y=0,
        )
    )
    db.flush()
    out = review_settled_misses(db, limit=5, league_id="nfl")
    db.flush()
    assert out["applied"] is False
    assert out["retrain"] is False
    assert out["play_enabled"] is False
    row = db.query(AiReview).filter(AiReview.forecast_id == "bp-settle").one_or_none()
    assert row is not None, out
    assert row.applied is False
    assert row.origin == "playing_time"
    assert out["reviews_written"] >= 1


def test_review_suggests_an_experiment_and_does_not_apply_it(db):
    assert classify_miss(0.7, 0, availability=0.2) == "injuries"
    assert classify_miss(0.7, 0) == "workload"
    assert classify_miss(0.52, 0.5) == "random_variation"
    db.add(
        BacktestRun(
            run_id="bt1",
            league_id="nfl",
            market_id="anytime_td",
            model_family="naive",
            train_end=NOW,
            test_start=NOW,
            test_end=NOW,
            n=1,
        )
    )
    db.flush()
    db.add(
        BacktestPrediction(
            id="bp1",
            run_id="bt1",
            game_id="g1",
            as_of=NOW,
            train_cutoff=NOW,
            p=0.8,
            y=0,
        )
    )
    db.flush()
    assert review_backtests(db) == 1
    db.flush()
    row = db.query(AiReview).one()
    assert row.applied is False
    assert row.origin == "workload"
    assert review_backtests(db) == 0
