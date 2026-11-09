"""Player Anytime TD package.

- Phase 3 allocator (team λ × shares): ``train_player_td_models`` / ``score_upcoming_player_td``
- Parallel m6 opportunity research study: ``run_player_td_study`` (isolated artifacts)

Research-only. Never promotes. Never enables Validated Play.
"""

from spe.models.player_td.integration import TeamScoringContext, integration_requirements
from spe.models.player_td.score import score_upcoming_player_td
from spe.models.player_td.study import run_player_td_study
from spe.models.player_td.train import train_player_td_models

__all__ = [
    "train_player_td_models",
    "score_upcoming_player_td",
    "run_player_td_study",
    "TeamScoringContext",
    "integration_requirements",
]
