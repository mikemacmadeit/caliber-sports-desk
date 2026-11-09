"""Prospective shadow testing. Research only. Play stays disabled."""

from spe.shadow.capture import capture_shadow_predictions
from spe.shadow.jobs import run_shadow_cycle
from spe.shadow.report import shadow_track_record
from spe.shadow.settle import settle_shadow_predictions

__all__ = [
    "capture_shadow_predictions",
    "settle_shadow_predictions",
    "shadow_track_record",
    "run_shadow_cycle",
]
