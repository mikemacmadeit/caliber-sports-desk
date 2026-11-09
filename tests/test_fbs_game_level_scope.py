"""Regression: FBS C_recent game-level scope vs involved-conditional backup usage."""

from __future__ import annotations

from spe.research.fbs_game_level_scope import (
    GAME_LEVEL,
    INVOLVED_CONDITIONAL,
    ROLE_UNKNOWN,
    PlayerTeamSeasonUsage,
    apply_scope_to_research_player,
    classify_fbs_projection_scope,
)


def _ctx(
    *,
    team_final: dict[str, int],
    usage: dict[tuple[str, str], PlayerTeamSeasonUsage],
    qb_leader: dict[str, dict],
    season_year: int = 2026,
) -> dict:
    return {
        "usage": usage,
        "team_final": team_final,
        "qb_leader": qb_leader,
        "season_year": season_year,
    }


def test_starter_qb_remains_game_level():
    usage = {
        ("qb1", "teamA"): PlayerTeamSeasonUsage(
            player_id="qb1",
            team_id="teamA",
            season_year=2026,
            team_final_games=8,
            involved_games=8,
            total_attempts=240,
            total_carries=40,
            position_group="QB",
        )
    }
    ctx = _ctx(
        team_final={"teamA": 8},
        usage=usage,
        qb_leader={"teamA": {"player_id": "qb1", "attempts": 240}},
    )
    scope = classify_fbs_projection_scope(
        player_id="qb1", team_id="teamA", position_group="QB", ctx=ctx
    )
    assert scope["game_level_supported"] is True
    assert scope["projection_scope"] == GAME_LEVEL


def test_backup_qb_with_documented_situational_usage_not_game_level():
    """Deuce Knight–shaped: relief QB, 3/8 involved, teammate starter attempts dominate."""
    usage = {
        ("starter", "olemiss"): PlayerTeamSeasonUsage(
            player_id="starter",
            team_id="olemiss",
            season_year=2026,
            team_final_games=8,
            involved_games=8,
            total_attempts=296,
            total_carries=48,
            position_group="QB",
        ),
        ("knight", "olemiss"): PlayerTeamSeasonUsage(
            player_id="knight",
            team_id="olemiss",
            season_year=2026,
            team_final_games=8,
            involved_games=3,
            total_attempts=8,
            total_carries=11,
            position_group="QB",
        ),
    }
    ctx = _ctx(
        team_final={"olemiss": 8},
        usage=usage,
        qb_leader={"olemiss": {"player_id": "starter", "attempts": 296}},
    )
    scope = classify_fbs_projection_scope(
        player_id="knight", team_id="olemiss", position_group="QB", ctx=ctx
    )
    assert scope["game_level_supported"] is False
    assert scope["projection_scope"] == INVOLVED_CONDITIONAL
    assert "documented_backup_qb_attempt_share" in scope["reason_codes"]
    assert "situational_current_team_involvement" in scope["reason_codes"]

    row = apply_scope_to_research_player(
        {
            "player_id": "knight",
            "p_c_recent": 0.48,
            "p_m5": 0.47,
            "pipeline_version": "fbs_m5_research_refit_v1+recent8_prior_td_rate",
        },
        scope,
    )
    assert row["suppress_game_level_primary"] is True
    assert row["p_c_recent"] == 0.48  # raw preserved; not rewritten
    assert "involved_conditional_not_game_level" in {
        w["code"] for w in row["research_warnings"]
    }


def test_confirmed_unavailable_stays_distinct_from_scope_demotion():
    """Unavailable estimates remain unavailable; scope does not invent a substitute."""
    usage = {
        ("rb1", "teamA"): PlayerTeamSeasonUsage(
            player_id="rb1",
            team_id="teamA",
            season_year=2026,
            team_final_games=8,
            involved_games=1,
            total_attempts=0,
            total_carries=3,
            position_group="RB",
        )
    }
    ctx = _ctx(
        team_final={"teamA": 8},
        usage=usage,
        qb_leader={},
    )
    scope = classify_fbs_projection_scope(
        player_id="rb1", team_id="teamA", position_group="RB", ctx=ctx
    )
    row = apply_scope_to_research_player(
        {"player_id": "rb1", "p_c_recent": None, "c_recent_unavailable_reason": "scoring_failed"},
        scope,
    )
    assert row.get("p_c_recent") is None
    assert row["suppress_game_level_primary"] is True


def test_unknown_role_insufficient_team_games():
    ctx = _ctx(team_final={"teamA": 2}, usage={}, qb_leader={})
    scope = classify_fbs_projection_scope(
        player_id="new", team_id="teamA", position_group="RB", ctx=ctx
    )
    assert scope["game_level_supported"] is False
    assert scope["projection_scope"] == ROLE_UNKNOWN
    assert "insufficient_current_team_season_evidence" in scope["reason_codes"]
