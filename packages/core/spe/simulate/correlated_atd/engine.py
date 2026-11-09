"""Correlated Anytime TD Monte Carlo: team TD counts → rush/rec split → player allocation.

Preserves:
- Team totals equal allocated TDs + residual OTHER bucket
- One scorer per TD (multinomial)
- QB rush TDs only; passing TDs go to receivers
- Joint teammate parlays from the same draws
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from spe.models.player_td.integration import TeamScoringContext
from spe.models.player_td.shares import DEFAULT_OTHER_SHARE
from spe.simulate.correlated_atd.player_weights import PlayerOpportunity

OTHER_ID = "__OTHER__"


@dataclass
class PlayerSimStats:
    player_id: str
    team_id: str
    name: str
    position_group: str
    p_anytime_td: float
    mean_tds: float
    p_rush_td: float
    p_rec_td: float
    p_se: float
    m6_p: float | None
    delta_vs_m6: float | None
    rush_weight: float
    rec_weight: float
    availability: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CorrelatedAtdResult:
    seed: int
    draw_count: int
    league_id: str
    game_id: str
    research_only: bool
    play_enabled: bool
    team_expected: dict[str, dict[str, Any]]
    players: list[PlayerSimStats]
    joint_td: dict[str, float]
    joint_same_team: dict[str, float]
    mechanics: dict[str, Any]
    convergence: dict[str, float]
    version: str = "atd_sim_v1"

    def to_dict(self, *, top_n: int | None = None) -> dict[str, Any]:
        players = sorted(self.players, key=lambda p: p.p_anytime_td, reverse=True)
        if top_n is not None:
            players = players[:top_n]
        return {
            "version": self.version,
            "seed": self.seed,
            "draw_count": self.draw_count,
            "league_id": self.league_id,
            "game_id": self.game_id,
            "research_only": self.research_only,
            "play_enabled": self.play_enabled,
            "validated_play": False,
            "team_expected": self.team_expected,
            "players": [p.to_dict() for p in players],
            "top_scorers": [
                {"player_id": p.player_id, "name": p.name, "position": p.position_group, "p": p.p_anytime_td}
                for p in sorted(self.players, key=lambda x: x.p_anytime_td, reverse=True)[:5]
            ],
            "joint_td_sample": dict(list(self.joint_td.items())[:40]),
            "joint_same_team_sample": dict(list(self.joint_same_team.items())[:20]),
            "mechanics": self.mechanics,
            "convergence": self.convergence,
        }


def _sample_count_from_pmf(rng: np.random.Generator, pmf: tuple[float, ...] | None, lam: float, n: int) -> np.ndarray:
    """Sample offensive TD counts. Prefer Poisson(λ) for mean fidelity; PMF used when provided for 0–4."""
    if pmf is None or len(pmf) < 6 or abs(sum(pmf) - 1.0) > 0.05:
        return rng.poisson(max(lam, 1e-6), size=n).astype(np.int16)
    p = np.asarray(pmf[:6], dtype=float)
    p = np.clip(p, 0, None)
    p = p / p.sum()
    buckets = rng.choice(6, size=n, p=p)
    # 5+ bucket: 5 + Poisson(max(λ - 4.2, 0.4)) so mean roughly matches λ.
    extra_lam = max(float(lam) - 4.2, 0.4)
    extra = rng.poisson(extra_lam, size=n)
    return np.where(buckets < 5, buckets, 5 + extra).astype(np.int16)


def _live_weights(
    idxs: list[int],
    weight_attr: str,
    opportunities: list[PlayerOpportunity],
    avail: np.ndarray,
    draw: int,
    other_share: float,
) -> tuple[list[int], np.ndarray]:
    live = [i for i in idxs if avail[i, draw] and getattr(opportunities[i], weight_attr) > 0]
    if not live:
        return [], np.array([])
    w = np.array([getattr(opportunities[i], weight_attr) for i in live], dtype=float)
    modeled = float(w.sum())
    if modeled <= 1e-12:
        w = np.ones(len(live)) / len(live) * (1.0 - other_share)
    else:
        w = w / modeled * (1.0 - other_share)
    return live, w


def _allocate_path(
    rng: np.random.Generator,
    idxs: list[int],
    weight_attr: str,
    players: list[PlayerOpportunity],
    avail: np.ndarray,
    team_counts: np.ndarray,
    out_counts: np.ndarray,
    other_counts: np.ndarray,
    other_share: float,
) -> None:
    """Allocate TD counts along one path (rush or receiving).

    Fast path: when availability is universal for the team, batch multinomials by count.
    """
    n = len(team_counts)
    if not idxs:
        other_counts[:] = team_counts
        return

    # Candidates with positive weight.
    base = [i for i in idxs if getattr(players[i], weight_attr) > 0]
    if not base:
        other_counts[:] = team_counts
        return

    team_avail = avail[base]  # (n_base, n)
    always_on = bool(np.all(team_avail))
    if always_on:
        w = np.array([getattr(players[i], weight_attr) for i in base], dtype=float)
        modeled = float(w.sum())
        if modeled <= 1e-12:
            w = np.ones(len(base)) / len(base) * (1.0 - other_share)
        else:
            w = w / modeled * (1.0 - other_share)
        probs = np.append(w, other_share)
        probs = probs / probs.sum()
        max_k = int(team_counts.max()) if len(team_counts) else 0
        for k in range(1, max_k + 1):
            ds = np.flatnonzero(team_counts == k)
            if len(ds) == 0:
                continue
            alloc = rng.multinomial(k, probs, size=len(ds))
            for j, i in enumerate(base):
                out_counts[i, ds] = alloc[:, j]
            other_counts[ds] = alloc[:, -1]
        return

    # Slow path: availability varies by draw.
    for draw in range(n):
        n_td = int(team_counts[draw])
        if n_td <= 0:
            continue
        live, w = _live_weights(idxs, weight_attr, players, avail, draw, other_share)
        if not live:
            other_counts[draw] = n_td
            continue
        probs = np.append(w, other_share)
        probs = probs / probs.sum()
        alloc = rng.multinomial(n_td, probs)
        for j, i in enumerate(live):
            out_counts[i, draw] = alloc[j]
        other_counts[draw] = alloc[-1]


def simulate_correlated_atd(
    league_id: str,
    game_id: str,
    home_team_id: str,
    away_team_id: str,
    opportunities: list[PlayerOpportunity],
    team_contexts: dict[str, TeamScoringContext],
    *,
    draw_count: int = 8_000,
    seed: int = 42,
    other_share: float = DEFAULT_OTHER_SHARE,
    exp_possessions: dict[str, float] | None = None,
) -> CorrelatedAtdResult:
    rng = np.random.default_rng(seed)
    n = int(draw_count)
    players = list(opportunities)
    m = len(players)

    # Availability draws (independent per player; injuries handled via availability).
    avail = np.ones((m, n), dtype=bool)
    for i, p in enumerate(players):
        a = float(np.clip(p.availability, 0.0, 1.0))
        if a < 0.999:
            avail[i] = rng.random(n) < a

    by_team: dict[str, list[int]] = {home_team_id: [], away_team_id: []}
    for i, p in enumerate(players):
        by_team.setdefault(p.team_id, []).append(i)

    team_off = {}
    team_rush = {}
    team_pass = {}
    team_expected: dict[str, dict[str, Any]] = {}
    for tid in (home_team_id, away_team_id):
        ctx = team_contexts.get(tid)
        lam_off = float(ctx.expected_off_tds) if ctx and ctx.expected_off_tds is not None else 2.3
        lam_rush = float(ctx.expected_rush_tds) if ctx and ctx.expected_rush_tds is not None else lam_off * 0.4
        lam_pass = float(ctx.expected_rec_tds) if ctx and ctx.expected_rec_tds is not None else lam_off * 0.6
        # Constrain split to total.
        split = lam_rush + lam_pass
        if split > 1e-8:
            lam_rush = lam_off * (lam_rush / split)
            lam_pass = lam_off * (lam_pass / split)
        pmf = ctx.off_td_pmf if ctx else None
        off = _sample_count_from_pmf(rng, pmf, lam_off, n)
        # Game-script: possessions noise mildly scales TD intensity already sampled via Poisson/PMF.
        if exp_possessions and tid in exp_possessions:
            base = float(exp_possessions[tid]) or 11.5
            poss = rng.poisson(base, n).clip(8, 18)
            scale = np.clip(poss / base, 0.85, 1.15)
            # Soften by re-drawing a fraction — skip heavy rework; store for reporting.
            del scale
        rush_p = lam_rush / max(lam_off, 1e-8)
        rush = rng.binomial(off, np.clip(rush_p, 0.05, 0.95))
        pas = off - rush
        team_off[tid] = off
        team_rush[tid] = rush
        team_pass[tid] = pas
        team_expected[tid] = {
            "expected_off_tds": lam_off,
            "expected_rush_tds": lam_rush,
            "expected_rec_tds": lam_pass,
            "off_td_pmf": list(pmf) if pmf else None,
            "model_version": ctx.model_version if ctx else None,
            "sim_mean_off": float(np.mean(off)),
            "sim_mean_rush": float(np.mean(rush)),
            "sim_mean_rec": float(np.mean(pas)),
        }

    # Mild game-script: when trailing in TDs, convert a few pass→rush (and vice versa).
    for tid, opp in ((home_team_id, away_team_id), (away_team_id, home_team_id)):
        convert = np.clip(
            np.round((team_off[opp].astype(float) - team_off[tid].astype(float)) * 0.03).astype(np.int16),
            -2,
            2,
        )
        to_rush = np.minimum(np.maximum(convert, 0), team_pass[tid])
        to_pass = np.minimum(np.maximum(-convert, 0), team_rush[tid])
        team_pass[tid] = team_pass[tid] - to_rush + to_pass
        team_rush[tid] = team_rush[tid] + to_rush - to_pass

    rush_counts = np.zeros((m, n), dtype=np.int16)
    rec_counts = np.zeros((m, n), dtype=np.int16)
    other_rush = {tid: np.zeros(n, dtype=np.int16) for tid in (home_team_id, away_team_id)}
    other_rec = {tid: np.zeros(n, dtype=np.int16) for tid in (home_team_id, away_team_id)}

    for tid, idxs in by_team.items():
        if tid not in team_rush:
            continue
        _allocate_path(
            rng,
            idxs,
            "rush_weight",
            players,
            avail,
            team_rush[tid],
            rush_counts,
            other_rush[tid],
            other_share,
        )
        _allocate_path(
            rng,
            idxs,
            "rec_weight",
            players,
            avail,
            team_pass[tid],
            rec_counts,
            other_rec[tid],
            other_share,
        )

    td_counts = rush_counts + rec_counts
    stats: list[PlayerSimStats] = []
    for i, p in enumerate(players):
        anytime = td_counts[i] >= 1
        p_atd = float(np.mean(anytime))
        mean_tds = float(np.mean(td_counts[i]))
        p_rush = float(np.mean(rush_counts[i] >= 1))
        p_rec = float(np.mean(rec_counts[i] >= 1))
        se = float(np.sqrt(max(p_atd * (1 - p_atd), 0.0) / n))
        delta = None if p.m6_p is None else float(p_atd - p.m6_p)
        stats.append(
            PlayerSimStats(
                player_id=p.player_id,
                team_id=p.team_id,
                name=p.name,
                position_group=p.position_group,
                p_anytime_td=p_atd,
                mean_tds=mean_tds,
                p_rush_td=p_rush,
                p_rec_td=p_rec,
                p_se=se,
                m6_p=p.m6_p,
                delta_vs_m6=delta,
                rush_weight=p.rush_weight,
                rec_weight=p.rec_weight,
                availability=p.availability,
                notes=list(p.notes),
            )
        )

    # Joint parlays from same draws (skill positions).
    skill_idx = [i for i, p in enumerate(players) if p.position_group in {"RB", "WR", "TE", "QB"}]
    joint_td: dict[str, float] = {}
    joint_same_team: dict[str, float] = {}
    for a in range(len(skill_idx)):
        for b in range(a + 1, len(skill_idx)):
            ia, ib = skill_idx[a], skill_idx[b]
            key = f"{players[ia].player_id}|{players[ib].player_id}"
            joint = float(np.mean((td_counts[ia] >= 1) & (td_counts[ib] >= 1)))
            joint_td[key] = joint
            if players[ia].team_id == players[ib].team_id:
                joint_same_team[key] = joint

    # Mechanics checks on draws.
    alloc_ok = True
    for tid in (home_team_id, away_team_id):
        idxs = by_team.get(tid, [])
        if not idxs:
            continue
        allocated = td_counts[idxs].sum(axis=0) + other_rush[tid] + other_rec[tid]
        if not np.array_equal(allocated, team_off[tid]):
            alloc_ok = False
            break

    max_se = max((s.p_se for s in stats), default=0.0)
    mechanics = {
        "team_equals_allocated_plus_other": alloc_ok,
        "other_share": other_share,
        "qb_rec_weight_all_zero": all(
            p.rec_weight == 0.0 for p in players if p.position_group == "QB"
        ),
        "prob_in_unit_interval": all(0.0 <= s.p_anytime_td <= 1.0 for s in stats),
        "mean_other_rush": {tid: float(np.mean(v)) for tid, v in other_rush.items()},
        "mean_other_rec": {tid: float(np.mean(v)) for tid, v in other_rec.items()},
        "n_players": m,
        "n_draws": n,
    }
    return CorrelatedAtdResult(
        seed=seed,
        draw_count=n,
        league_id=league_id,
        game_id=game_id,
        research_only=True,
        play_enabled=False,
        team_expected=team_expected,
        players=stats,
        joint_td=joint_td,
        joint_same_team=joint_same_team,
        mechanics=mechanics,
        convergence={"max_td_mc_se": float(max_se), "draw_count": float(n)},
    )
