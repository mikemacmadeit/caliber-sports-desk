from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PlayerObservation:
    player_id: str
    team_id: str
    conference: str | None
    position_group: str
    league_id: str
    games: int
    count_sum: float  # TDs, receptions, or yards depending on family
    exposure: float  # games or expected opportunities
    availability: float = 1.0


@dataclass
class HierarchicalPrediction:
    mean: float
    p_low: float
    p_high: float
    shrinkage: float
    components: dict[str, float]


class HierarchicalCountModel:
    """Empirical-Bayes Poisson/NB with league-specific shrinkage.

    NFL and FBS MUST be separate instances. FBS shrinks further toward
    team/conference and opponent-strength priors because player samples are sparse.
    """

    def __init__(self, league_id: str, family: str = "td", extra_fbs_shrink: bool = False):
        self.league_id = league_id
        self.family = family
        self.extra_fbs_shrink = extra_fbs_shrink or league_id == "fbs"
        self.league_rate = 0.0
        self.team_rates: dict[str, float] = {}
        self.conf_rates: dict[str, float] = {}
        self.pos_rates: dict[str, float] = {}
        self.player_rates: dict[str, float] = {}
        self.player_n: dict[str, int] = {}
        self.k_player = 8.0 if league_id == "nfl" else 14.0
        self.k_team = 12.0 if league_id == "nfl" else 8.0
        self.fitted = False

    def fit(self, rows: list[PlayerObservation]) -> None:
        rows = [r for r in rows if r.league_id == self.league_id and r.exposure > 0]
        if not rows:
            raise ValueError(f"No observations for league {self.league_id}")
        tot_c = sum(r.count_sum for r in rows)
        tot_e = sum(r.exposure for r in rows)
        self.league_rate = tot_c / tot_e

        def _group(key_fn) -> dict[str, float]:
            acc: dict[str, list[float]] = {}
            for r in rows:
                k = key_fn(r)
                if not k:
                    continue
                acc.setdefault(k, [0.0, 0.0])
                acc[k][0] += r.count_sum
                acc[k][1] += r.exposure
            out: dict[str, float] = {}
            for k, (c, e) in acc.items():
                raw = c / e
                shrink = e / (e + self.k_team)
                out[k] = shrink * raw + (1 - shrink) * self.league_rate
            return out

        self.team_rates = _group(lambda r: r.team_id)
        self.conf_rates = _group(lambda r: r.conference or "")
        self.pos_rates = _group(lambda r: r.position_group)

        for r in rows:
            prior = self._prior(r.team_id, r.conference, r.position_group)
            shrink = r.exposure / (r.exposure + self.k_player)
            raw = r.count_sum / r.exposure
            self.player_rates[r.player_id] = shrink * raw + (1 - shrink) * prior
            self.player_n[r.player_id] = r.games
        self.fitted = True

    def _prior(self, team_id: str, conference: str | None, position_group: str) -> float:
        parts = [self.league_rate]
        weights = [1.0]
        if team_id in self.team_rates:
            parts.append(self.team_rates[team_id])
            weights.append(2.0)
        if self.extra_fbs_shrink and conference and conference in self.conf_rates:
            parts.append(self.conf_rates[conference])
            weights.append(1.5)
        if position_group in self.pos_rates:
            parts.append(self.pos_rates[position_group])
            weights.append(1.2)
        w = np.array(weights)
        return float(np.average(parts, weights=w))

    def predict_rate(
        self,
        player_id: str,
        team_id: str,
        conference: str | None,
        position_group: str,
        *,
        availability: float = 1.0,
        game_total_mult: float = 1.0,
        sample_games: int | None = None,
    ) -> HierarchicalPrediction:
        if not self.fitted:
            raise RuntimeError("Model is not fitted")
        prior = self._prior(team_id, conference, position_group)
        if player_id in self.player_rates:
            n = sample_games if sample_games is not None else self.player_n.get(player_id, 0)
            shrink = n / (n + self.k_player)
            rate = self.player_rates[player_id]
        else:
            shrink = 0.0
            n = 0
            rate = prior
        mean = max(1e-6, rate * availability * game_total_mult)
        # Wide intervals when sample is thin — especially FBS backups.
        se = np.sqrt(mean / max(n, 1)) * (1.4 if self.extra_fbs_shrink else 1.0)
        return HierarchicalPrediction(
            mean=mean,
            p_low=max(1e-6, mean - 1.64 * se),
            p_high=mean + 1.64 * se,
            shrinkage=float(shrink),
            components={
                "league_rate": self.league_rate,
                "team_rate": self.team_rates.get(team_id, prior),
                "player_rate": self.player_rates.get(player_id, prior),
                "availability": availability,
                "game_total_mult": game_total_mult,
            },
        )

    @staticmethod
    def poisson_at_least_one(lam: float) -> float:
        return float(1.0 - np.exp(-lam))

    @staticmethod
    def poisson_over(lam: float, line: float) -> float:
        """P(X > line) for a .5 line (no push)."""
        k = int(np.floor(line))
        # survival: 1 - F(k)
        p = 0.0
        term = np.exp(-lam)
        for i in range(k + 1):
            p += term
            term *= lam / (i + 1)
        return float(max(0.0, min(1.0, 1.0 - p)))
