"""Against-the-spread prediction system. Separate from Anytime TD models."""

from spe.models.ats.backtest import run_ats_backtest
from spe.models.ats.predict import predict_upcoming_ats
from spe.models.ats.ratings import build_team_strength_frame

__all__ = [
    "build_team_strength_frame",
    "predict_upcoming_ats",
    "run_ats_backtest",
]
