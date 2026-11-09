"""Clean interface for Agent 1 team-TD distributions → player Anytime TD.

This module does **not** import or retrain ``spe.models.team_td``.
It only defines the contract Agent 2 will eventually consume once simulation
wiring (Phase 3 / shared sim) begins.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class TeamScoringContext:
    """Expected team scoring supplied by Agent 1 (or a stub).

    All fields are optional so player models remain usable before integration.
    Uncertainty should be expressed as a distribution over offensive TDs when
    available — never as fabricated player shares.
    """

    game_id: str
    team_id: str
    league_id: str
    expected_off_tds: float | None = None
    expected_rush_tds: float | None = None
    expected_rec_tds: float | None = None
    # P(K=k) for k=0..4 and 5+ when Agent 1 exposes buckets.
    off_td_pmf: tuple[float, ...] | None = None
    train_end: str | None = None
    model_version: str | None = None
    research_only: bool = True
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TeamScoringProvider(Protocol):
    """Agent 1 implements this (or a thin adapter around team_td artifacts)."""

    def team_scoring_for_game(self, game_id: str, team_id: str) -> TeamScoringContext | None: ...


@dataclass
class PlayerTdRequest:
    """Inputs for a single player Anytime TD probability."""

    game_id: str
    player_id: str
    team_id: str
    opponent_id: str
    position_group: str
    league_id: str
    as_of: str
    player_features: dict[str, float] = field(default_factory=dict)
    team_scoring: TeamScoringContext | None = None


@dataclass
class PlayerTdPrediction:
    player_id: str
    game_id: str
    p_anytime_td: float
    p_low: float
    p_high: float
    mean_tds: float
    model_version: str
    position_group: str
    research_only: bool = True
    play_enabled: bool = False
    used_team_scoring: bool = False
    components: dict[str, Any] = field(default_factory=dict)


def blend_with_team_scoring(
    base_mean_tds: float,
    position_group: str,
    team: TeamScoringContext | None,
    *,
    historical_share: float | None = None,
) -> tuple[float, dict[str, Any]]:
    """Optional mean adjustment when Agent 1 context is present.

    Does not invent a share. If ``historical_share`` is missing, returns the
    base mean unchanged and records that team scoring was unused for allocation.
    """
    meta: dict[str, Any] = {"base_mean": base_mean_tds, "used_team_scoring": False}
    if team is None:
        return base_mean_tds, meta
    meta["team_context"] = {
        "expected_off_tds": team.expected_off_tds,
        "expected_rush_tds": team.expected_rush_tds,
        "expected_rec_tds": team.expected_rec_tds,
        "model_version": team.model_version,
    }
    if historical_share is None or not (0.0 <= historical_share <= 1.0):
        meta["reason"] = "no_verified_player_share"
        return base_mean_tds, meta

    # Route team expected TDs by position mechanism.
    if position_group == "QB":
        team_exp = team.expected_rush_tds
    elif position_group == "RB":
        # RBs score both ways; prefer total when rush/rec split missing.
        if team.expected_rush_tds is not None and team.expected_rec_tds is not None:
            team_exp = 0.75 * team.expected_rush_tds + 0.25 * team.expected_rec_tds
        else:
            team_exp = team.expected_off_tds
    else:
        team_exp = team.expected_rec_tds if team.expected_rec_tds is not None else team.expected_off_tds

    if team_exp is None:
        meta["reason"] = "team_expectation_missing_for_path"
        return base_mean_tds, meta

    allocated = float(historical_share) * float(team_exp)
    # Conservative blend — do not fully replace player history.
    blended = 0.5 * base_mean_tds + 0.5 * allocated
    meta.update(
        {
            "used_team_scoring": True,
            "historical_share": historical_share,
            "team_exp_used": team_exp,
            "allocated": allocated,
            "blended_mean": blended,
        }
    )
    return blended, meta


def integration_requirements() -> dict[str, Any]:
    return {
        "player_model_accepts": [
            "expected_off_tds",
            "expected_rush_tds",
            "expected_rec_tds",
            "off_td_pmf (optional)",
            "team scoring uncertainty via pmf or predictive intervals",
        ],
        "player_model_requires_from_self": [
            "verified prior player share of rush/rec TDs (not invented)",
            "position path (QB rush-only for Anytime TD)",
        ],
        "must_not": [
            "rebuild Agent 1 team models inside player_td",
            "connect simulator yet",
            "promote or enable Validated Play",
            "overwrite shared artifacts",
        ],
        "adapter_suggestion": (
            "Thin adapter: load Agent 1 nfl_team_td_v1 / fbs_team_td_v1 predictions "
            "into TeamScoringContext; pass into PlayerTdRequest.team_scoring."
        ),
        "protocol": "TeamScoringProvider.team_scoring_for_game(game_id, team_id)",
    }
