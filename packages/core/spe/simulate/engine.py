from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spe.simulate.rules import rules_for


@dataclass
class PlayerRole:
    player_id: str
    team_id: str
    position_group: str  # QB|RB|WR|TE|K|DEF
    availability: float
    snap_share: float
    td_weight: float
    rec_lambda: float
    rec_yards_mu: float
    rush_yards_mu: float


@dataclass
class SimulationResult:
    seed: int
    draw_count: int
    league_id: str
    research_only: bool
    overrides: dict[str, Any]
    player_td_prob: dict[str, float]
    player_rec_over: dict[str, dict[str, float]]
    player_yards_over: dict[str, dict[str, float]]
    score_quantiles: dict[str, list[float]]
    joint_td: dict[str, float]
    convergence: dict[str, float]
    draws: np.ndarray | None = field(default=None, repr=False)


def simulate_game(
    league_id: str,
    home_team_id: str,
    away_team_id: str,
    players: list[PlayerRole],
    *,
    expected_home_score: float = 24.0,
    expected_away_score: float = 22.0,
    expected_drives: float = 22.0,
    draw_count: int = 10_000,
    seed: int = 42,
    overrides: dict[str, Any] | None = None,
    rec_lines: dict[str, float] | None = None,
    yard_lines: dict[str, tuple[str, float]] | None = None,
    persist_draws: bool = False,
) -> SimulationResult:
    """Correlated Monte Carlo. Joint parlays MUST be counted from the same draws.

    A scenario with 10k well-calibrated draws is preferred to 1M weak ones.
    """
    rules = rules_for(league_id)
    rng = np.random.default_rng(seed)
    overrides = dict(overrides or {})
    research_only = bool(overrides)
    rec_lines = rec_lines or {}
    yard_lines = yard_lines or {}

    n = draw_count
    avail = np.array(
        [
            rng.random(n) < np.clip(p.availability, 0, 1)
            if p.availability < 0.999
            else np.ones(n, dtype=bool)
            for p in players
        ]
    )

    # Shared game-script: drive counts and team totals (negative binomial-ish via gamma-poisson).
    drives = rng.poisson(expected_drives, n).clip(14, 32)
    home_pts = rng.gamma(expected_home_score / 4.0, 4.0, n)
    away_pts = rng.gamma(expected_away_score / 4.0, 4.0, n)
    # Pace/script correlation: more drives → slightly more points and receptions.
    script = (drives - expected_drives) / 8.0

    home_tds = np.clip(np.round(home_pts / 7.0), 0, 8).astype(int)
    away_tds = np.clip(np.round(away_pts / 7.0), 0, 8).astype(int)
    # About 12% of TDs are defensive/ST — excluded from offensive player allocation.
    off_share = 0.88
    home_off_td = np.maximum(rng.binomial(home_tds, off_share), 0)
    away_off_td = np.maximum(rng.binomial(away_tds, off_share), 0)

    by_team: dict[str, list[int]] = {home_team_id: [], away_team_id: []}
    for i, p in enumerate(players):
        by_team.setdefault(p.team_id, []).append(i)

    td_counts = np.zeros((len(players), n), dtype=np.int16)
    recs = np.zeros((len(players), n), dtype=np.float32)
    rec_yds = np.zeros((len(players), n), dtype=np.float32)
    rush_yds = np.zeros((len(players), n), dtype=np.float32)

    for team_id, idxs in by_team.items():
        team_tds = home_off_td if team_id == home_team_id else away_off_td
        weights = np.array([max(players[i].td_weight, 1e-6) for i in idxs], dtype=float)
        # Dirichlet-multinomial allocation so teammate TDs compete.
        for draw in range(n):
            live = [i for i in idxs if avail[i, draw] and players[i].position_group in {"RB", "WR", "TE", "QB"}]
            if not live or team_tds[draw] == 0:
                continue
            w = np.array([players[i].td_weight * players[i].snap_share for i in live], dtype=float)
            w = w / w.sum()
            alloc = rng.multinomial(int(team_tds[draw]), w)
            for j, i in enumerate(live):
                td_counts[i, draw] = alloc[j]

        for i in idxs:
            p = players[i]
            lam = p.rec_lambda * (1 + 0.08 * script) * avail[i]
            recs[i] = rng.poisson(np.clip(lam, 0.01, 18))
            rec_yds[i] = rng.gamma(np.clip(p.rec_yards_mu / 8.0, 0.2, 20), 8.0) * avail[i] * (1 + 0.05 * script)
            rush_yds[i] = rng.gamma(np.clip(p.rush_yards_mu / 10.0, 0.2, 20), 10.0) * avail[i]

    player_td_prob = {
        players[i].player_id: float(np.mean(td_counts[i] >= 1)) for i in range(len(players))
    }
    player_rec_over: dict[str, dict[str, float]] = {}
    for pid, line in rec_lines.items():
        i = _index(players, pid)
        if i is None:
            continue
        player_rec_over[pid] = {str(line): float(np.mean(recs[i] > line))}
    player_yards_over: dict[str, dict[str, float]] = {}
    for pid, (kind, line) in yard_lines.items():
        i = _index(players, pid)
        if i is None:
            continue
        arr = rec_yds[i] if kind == "rec" else rush_yds[i]
        player_yards_over[pid] = {f"{kind}:{line}": float(np.mean(arr > line))}

    # Joint TD combinations for parlay evaluation (same draw set).
    skill = [i for i, p in enumerate(players) if p.position_group in {"RB", "WR", "TE"}]
    joint_td: dict[str, float] = {}
    for a in range(len(skill)):
        for b in range(a + 1, len(skill)):
            ia, ib = skill[a], skill[b]
            key = f"{players[ia].player_id}|{players[ib].player_id}"
            joint_td[key] = float(np.mean((td_counts[ia] >= 1) & (td_counts[ib] >= 1)))

    # Monte Carlo SE vs parameter uncertainty: report MC SE here.
    td_se = {
        players[i].player_id: float(np.sqrt(player_td_prob[players[i].player_id] * (1 - player_td_prob[players[i].player_id]) / n))
        for i in range(len(players))
    }
    max_se = max(td_se.values()) if td_se else 0.0

    result = SimulationResult(
        seed=seed,
        draw_count=n,
        league_id=rules.league_id,
        research_only=research_only,
        overrides=overrides,
        player_td_prob=player_td_prob,
        player_rec_over=player_rec_over,
        player_yards_over=player_yards_over,
        score_quantiles={
            "home": _quantiles(home_pts),
            "away": _quantiles(away_pts),
        },
        joint_td=joint_td,
        convergence={"max_td_mc_se": max_se, "draw_count": float(n), "target_se": 0.005},
        draws=np.vstack([td_counts, recs, rec_yds, rush_yds]) if persist_draws else None,
    )
    return result


def _quantiles(arr: np.ndarray) -> list[float]:
    return [float(x) for x in np.quantile(arr, [0.1, 0.25, 0.5, 0.75, 0.9])]


def _index(players: list[PlayerRole], player_id: str) -> int | None:
    for i, p in enumerate(players):
        if p.player_id == player_id:
            return i
    return None
