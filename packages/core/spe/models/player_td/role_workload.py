"""Role-and-workload Anytime TD candidate.

The chance starts from this week's role, the carries and targets that role
should get, goal-line work, and how many scores the team should create.
A past scoring rate is shrunk toward the rate for that role. A backup who
scored in a few games where he touched the ball does not keep that rate
next week. The shrink depends on how many games he actually worked, not on
a flat cut to his percentage.

This module only scores a prepared frame. It does not read the database,
write a model file, or change a frozen prediction.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from spe.models.player_td.shares import ShareWeights, attach_shares, default_weights

ROLE_STARTER = "starter"
ROLE_ROTATIONAL = "rotational"
ROLE_BACKUP = "backup"
ROLE_UNKNOWN = "unknown"

WORKLOAD_FIELDS = ("carries", "targets", "gl", "rz", "rush_td", "rec_td")
# Historical touchdowns stay in the candidate, at a lower weight than the
# current live share formula (1.5). They cannot outrank expected work.
HIST_TD_WEIGHT = 0.4
STARTER_SHRINK_GAMES = 2.0
ROTATIONAL_SHRINK_GAMES = 6.0


def shrink_toward(own: np.ndarray, base: np.ndarray, games: np.ndarray, strength: np.ndarray) -> np.ndarray:
    """Pull ``own`` toward ``base``. More real games of work means less pull."""
    own_v = np.asarray(own, dtype=float)
    base_v = np.asarray(base, dtype=float)
    games_v = np.clip(np.asarray(games, dtype=float), 0.0, None)
    strength_v = np.clip(np.asarray(strength, dtype=float), 0.0, None)
    denom = games_v + strength_v
    out = np.divide(strength_v * base_v + games_v * own_v, denom, out=base_v.copy(), where=denom > 0)
    return np.where(np.isfinite(out), out, base_v)


def shrink_strength(roles: pd.Series, backup_games: float) -> np.ndarray:
    """How many games of real work it takes before a player's own rate leads."""
    role = roles.fillna(ROLE_UNKNOWN).astype(str).to_numpy()
    out = np.full(len(role), float(backup_games), dtype=float)
    out[role == ROLE_STARTER] = STARTER_SHRINK_GAMES
    out[role == ROLE_ROTATIONAL] = ROTATIONAL_SHRINK_GAMES
    return out


def role_bases_lookup(frame: pd.DataFrame, bases: dict[tuple[str, str], dict[str, float]]) -> dict[str, np.ndarray]:
    positions = frame["position_group"].astype(str).to_numpy()
    roles = frame["role"].fillna(ROLE_UNKNOWN).astype(str).to_numpy()
    found: dict[str, list[float]] = {name: [] for name in WORKLOAD_FIELDS}
    for position, role in zip(positions, roles, strict=True):
        row = bases.get((position, role)) or bases.get((position, ROLE_BACKUP)) or {}
        for name in WORKLOAD_FIELDS:
            found[name].append(float(row.get(name) or 0.0))
    return {name: np.asarray(values, dtype=float) for name, values in found.items()}


def empirical_role_bases(frame: pd.DataFrame) -> dict[tuple[str, str], dict[str, float]]:
    """Typical recent workload for a position and role. Median, so one star does not set it."""
    columns = {
        "carries": "prior_carries",
        "targets": "prior_targets",
        "gl": "prior_gl_carries",
        "rz": "prior_rz_targets",
        "rush_td": "prior_rush_td",
        "rec_td": "prior_rec_td",
    }
    out: dict[tuple[str, str], dict[str, float]] = {}
    grouped = frame.groupby(["position_group", "role"], dropna=False)
    for (position, role), part in grouped:
        stats: dict[str, float] = {}
        for name, column in columns.items():
            if column not in part.columns or len(part) < 30:
                stats[name] = 0.0
                continue
            values = pd.to_numeric(part[column], errors="coerce").to_numpy(dtype=float)
            values = values[np.isfinite(values)]
            stats[name] = float(np.median(values)) if len(values) else 0.0
        out[(str(position), str(role))] = stats
    return out


def _count(frame: pd.DataFrame, name: str) -> np.ndarray:
    if name not in frame.columns:
        return np.zeros(len(frame), dtype=float)
    values = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float)
    return np.where(np.isfinite(values), values, 0.0)


def prepare_candidate_frame(
    frame: pd.DataFrame,
    bases: dict[tuple[str, str], dict[str, float]],
    *,
    backup_games: float,
) -> pd.DataFrame:
    """Replace raw history with the workload this role should get this week."""
    out = frame.copy()
    base = role_bases_lookup(out, bases)
    games = _count(out, "opportunity_games")
    if not np.any(games > 0):
        games = _count(out, "prior_games")
    strength = shrink_strength(out["role"] if "role" in out.columns else pd.Series(ROLE_UNKNOWN, index=out.index), backup_games)
    out["prior_carries"] = shrink_toward(_count(out, "prior_carries"), base["carries"], games, strength)
    out["prior_targets"] = shrink_toward(_count(out, "prior_targets"), base["targets"], games, strength)
    out["prior_gl_carries"] = shrink_toward(_count(out, "prior_gl_carries"), base["gl"], games, strength)
    out["prior_rz_targets"] = shrink_toward(_count(out, "prior_rz_targets"), base["rz"], games, strength)
    out["prior_rush_td"] = shrink_toward(_count(out, "prior_rush_td"), base["rush_td"], games, strength)
    out["prior_rec_td"] = shrink_toward(_count(out, "prior_rec_td"), base["rec_td"], games, strength)
    return out


def _probabilities_from_shares(scored: pd.DataFrame) -> np.ndarray:
    rush = _count(scored, "team_lambda_rush_td") * _count(scored, "rush_td_share")
    rec = _count(scored, "team_lambda_pass_td") * _count(scored, "rec_td_share")
    lam = np.clip(rush + rec, 0.0, None)
    return 1.0 - np.exp(-lam)


def score_role_workload(
    frame: pd.DataFrame,
    bases: dict[tuple[str, str], dict[str, float]],
    *,
    goal_line_weight: float,
    backup_games: float,
) -> np.ndarray:
    prepared = prepare_candidate_frame(frame, bases, backup_games=backup_games)
    weights = default_weights("nfl")
    weights.rush_gl = float(goal_line_weight)
    weights.rush_td_hist = HIST_TD_WEIGHT
    weights.rec_td_hist = HIST_TD_WEIGHT
    weights.rec_rz = 2.0
    return _probabilities_from_shares(attach_shares(prepared, weights))


def score_current_share(frame: pd.DataFrame, weights: ShareWeights | None = None) -> np.ndarray:
    """The live share formula: carries, goal-line carries, and historical scores, unshrunk."""
    return _probabilities_from_shares(attach_shares(frame, weights or default_weights("nfl")))


def score_historical_rate(frame: pd.DataFrame) -> np.ndarray:
    """Chance equals how often the player has scored before. No role, no workload."""
    rate = _count(frame, "prior_td_rate")
    return np.clip(rate, 0.0, 0.95)


def brier_score(y: np.ndarray, p: np.ndarray) -> float:
    actual = np.asarray(y, dtype=float)
    prob = np.clip(np.asarray(p, dtype=float), 1e-6, 1.0 - 1e-6)
    if len(actual) == 0:
        return float("nan")
    return float(np.mean((prob - actual) ** 2))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    actual = np.asarray(y, dtype=float)
    prob = np.clip(np.asarray(p, dtype=float), 1e-6, 1.0 - 1e-6)
    if len(actual) == 0:
        return float("nan")
    return float(-np.mean(actual * np.log(prob) + (1.0 - actual) * np.log(1.0 - prob)))


def metric_block(y: np.ndarray, p: np.ndarray) -> dict[str, Any]:
    return {
        "n": int(len(y)),
        "brier": brier_score(y, p),
        "log_loss": log_loss(y, p),
        "mean_p": float(np.mean(p)) if len(p) else None,
        "base_rate": float(np.mean(y)) if len(y) else None,
    }
