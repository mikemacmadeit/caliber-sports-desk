"""Phase 2: team offensive touchdown opportunity engine (research-only)."""

from spe.models.team_td.score import score_upcoming_team_td
from spe.models.team_td.train import train_team_td_models

__all__ = ["train_team_td_models", "score_upcoming_team_td"]
