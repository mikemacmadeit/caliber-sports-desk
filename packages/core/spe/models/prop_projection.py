"""Prop board projection from games the player actually played.

The saved hierarchical rate is a career shrink. For stars it sits well below
both recent production and the posted line, so the over probability collapses.
Once a player has a real pregame sample, the board uses that sample.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import Game, PlayerGameStat
from spe.models.hierarchical import HierarchicalCountModel

COUNTING_MARKETS = ("receptions", "receiving_yards", "rushing_yards")
STAT_COLUMN = {
    "receptions": "receptions",
    "receiving_yards": "rec_yards",
    "rushing_yards": "rush_yards",
}
MARKET_WORDS = {
    "receptions": "receptions",
    "receiving_yards": "receiving yards",
    "rushing_yards": "rushing yards",
    "anytime_td": "anytime touchdown",
}
PROJECTION_UNIT = {
    "receptions": "receptions",
    "receiving_yards": "receiving yards",
    "rushing_yards": "rush yards",
}
FALLBACK_SIGMA = {
    "receptions": 2.0,
    "receiving_yards": 28.0,
    "rushing_yards": 30.0,
}
RECENT_GAMES = 16
# Below this many played games, blend toward the saved model mean.
TRUST_RECENT_AT = 8
THIN_SHRINK_K = 4.0


@dataclass(frozen=True)
class PlayedGame:
    kickoff: datetime
    receptions: float
    rec_yards: float
    rush_yards: float


def pick_sentence(name: str | None, side: str | None, line: float | None, market_id: str | None) -> str:
    """One sentence that names the player, the side, the number, and the market."""
    who = (name or "Player").strip() or "Player"
    market = MARKET_WORDS.get(market_id or "", (market_id or "prop").replace("_", " "))
    if market_id == "anytime_td":
        return f"{who} anytime touchdown"
    side_word = (side or "over").replace("_", " ").lower()
    if line is None:
        return f"{who} {side_word} {market}"
    return f"{who} {side_word} {format(float(line), 'g')} {market}"


def mean_before(games: list[PlayedGame], kickoff: datetime | None, stat: str, window: int = RECENT_GAMES) -> tuple[float | None, int]:
    """Average of the stat in played games that kicked off before this one."""
    moment = as_utc(kickoff)
    if moment is None:
        return None, 0
    chosen: list[float] = []
    for game in games:
        played_at = as_utc(game.kickoff)
        if played_at is None or played_at >= moment:
            continue
        chosen.append(float(getattr(game, stat)))
        if len(chosen) >= window:
            break
    if not chosen:
        return None, 0
    return sum(chosen) / len(chosen), len(chosen)


def projection_mean(live: float | None, n: int, model_mean: float | None) -> tuple[float | None, str]:
    """Recent games stand on their own once the sample is long enough."""
    if live is None or n <= 0:
        return model_mean, "model"
    if n >= TRUST_RECENT_AT or model_mean is None:
        return float(live), "recent"
    weight = n / (n + THIN_SHRINK_K)
    return weight * float(live) + (1.0 - weight) * float(model_mean), "recent"


def over_probability(market_id: str, mean: float, line: float, sigma: float) -> float:
    if market_id == "receptions":
        return float(HierarchicalCountModel.poisson_over(mean, line))
    scale = sigma if sigma and sigma > 0 else FALLBACK_SIGMA.get(market_id, 28.0)
    return float(max(0.0, min(1.0, 1.0 - _norm_cdf(line, mean, scale))))


def side_probability(p_over: float, side: str | None) -> tuple[float, float, float]:
    p = float(max(1e-6, min(1.0 - 1e-6, p_over)))
    low = max(1e-6, p - 0.08)
    high = min(1.0, p + 0.08)
    if (side or "").lower() in {"under", "no"}:
        flipped = 1.0 - p
        return flipped, max(1e-6, 1.0 - high), min(1.0, 1.0 - low)
    return p, low, high


def model_lean(p: float | None) -> bool | None:
    if p is None:
        return None
    if abs(p - 0.5) < 1e-9:
        return None
    return p > 0.5


def logs_for_players(session: Session, player_ids: set[str] | list[str]) -> dict[str, list[PlayedGame]]:
    ids = [pid for pid in dict.fromkeys(player_ids) if pid]
    out: dict[str, list[PlayedGame]] = {pid: [] for pid in ids}
    if not ids:
        return out
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        rows = (
            session.query(
                PlayerGameStat.player_id,
                PlayerGameStat.attempts,
                PlayerGameStat.targets,
                PlayerGameStat.carries,
                PlayerGameStat.receptions,
                PlayerGameStat.rec_yards,
                PlayerGameStat.rush_yards,
                Game.kickoff_utc,
            )
            .join(Game, Game.game_id == PlayerGameStat.game_id)
            .filter(
                PlayerGameStat.player_id.in_(chunk),
                Game.status == "final",
                Game.kickoff_utc >= datetime.now(UTC).replace(tzinfo=None) - timedelta(days=800),
            )
            .all()
        )
        for player_id, attempts, targets, carries, receptions, rec_yards, rush_yards, kickoff in rows:
            if not _played(attempts, targets, carries, receptions, rec_yards, rush_yards):
                continue
            moment = as_utc(kickoff)
            if moment is None:
                continue
            out.setdefault(player_id, []).append(
                PlayedGame(
                    kickoff=moment,
                    receptions=float(receptions or 0),
                    rec_yards=float(rec_yards or 0),
                    rush_yards=float(rush_yards or 0),
                )
            )
    for games in out.values():
        games.sort(key=lambda game: game.kickoff, reverse=True)
    return out


def adjust_over_prediction(
    pred: dict[str, Any],
    *,
    market_id: str,
    line: float | None,
    sigma: float | None,
    kickoff: datetime | None,
    logs: list[PlayedGame],
    workload_factor: float = 1.0,
) -> dict[str, Any]:
    """Replace a counting-market mean with the pregame recent average. p stays P(over)."""
    if market_id not in STAT_COLUMN or line is None or pred.get("p") is None:
        return pred
    stat = STAT_COLUMN[market_id]
    live, n = mean_before(logs, kickoff, stat)
    mean, source = projection_mean(live, n, pred.get("mean"))
    if mean is None or source != "recent":
        return pred
    factor = float(workload_factor)
    if factor != 1.0 and factor > 0:
        mean = float(mean) * factor
    scale = float(sigma) if sigma else FALLBACK_SIGMA.get(market_id, 28.0)
    p_over = over_probability(market_id, mean, float(line), scale)
    out = dict(pred)
    out["mean"] = mean
    out["p"] = p_over
    out["p_low"] = max(1e-6, p_over - 0.08)
    out["p_high"] = min(1.0, p_over + 0.08)
    out["line"] = float(line)
    out["projection_games"] = n
    out["projection_source"] = source
    out["model_mean"] = pred.get("mean")
    out["workload_factor"] = factor
    return out


def annotate_prompt_forecasts(session: Session, league_id: str, kickoff: datetime | None, rows: list[dict[str, Any]]) -> None:
    """Put an explicit pick, and a recent-game probability, on each prompt forecast."""
    ids = {row.get("player_id") for row in rows if row.get("market_id") in STAT_COLUMN and row.get("player_id")}
    logs = logs_for_players(session, ids)
    for row in rows:
        row["pick"] = pick_sentence(row.get("player_name"), row.get("side"), row.get("line"), row.get("market_id"))
        if row.get("market_id") not in STAT_COLUMN or row.get("line") is None or not row.get("player_id"):
            continue
        live, n = mean_before(logs.get(row["player_id"], []), kickoff, STAT_COLUMN[row["market_id"]])
        mean, source = projection_mean(live, n, row.get("model_mean"))
        if mean is None or source != "recent":
            continue
        p, low, high = side_probability(
            over_probability(row["market_id"], mean, float(row["line"]), market_sigma(league_id, row["market_id"])),
            row.get("side"),
        )
        row["p"] = p
        row["p_low"] = low
        row["p_high"] = high
        row["projection"] = round(mean, 1)
        row["projection_games"] = n
        row["projection_unit"] = PROJECTION_UNIT[row["market_id"]]
        row["projection_source"] = source


def apply_row_projection(row: dict[str, Any], logs: dict[str, list[PlayedGame]]) -> None:
    """Name the pick on every row. Replace counting-market probabilities from recent games."""
    market_id = row.get("market_id")
    name = row.get("player_name")
    side = row.get("side")
    line = row.get("line")
    forecast = row.get("forecast") or {}
    row["pick"] = pick_sentence(name, side, line, market_id)
    if market_id not in STAT_COLUMN or line is None:
        row["model_lean"] = None
        row["projection"] = None
        row["projection_games"] = None
        row["projection_unit"] = None
        return
    player_id = row.get("player_id") or ""
    live, n = mean_before(logs.get(player_id, []), row.get("kickoff_utc"), STAT_COLUMN[market_id])
    model_mean = row.get("model_mean")
    if model_mean is None:
        model_mean = forecast.get("model_mean")
    mean, source = projection_mean(live, n, model_mean if isinstance(model_mean, (int, float)) else None)
    if mean is None or source != "recent":
        row["model_lean"] = model_lean(forecast.get("p_estimate"))
        row["projection"] = round(float(model_mean), 1) if isinstance(model_mean, (int, float)) else None
        row["projection_games"] = 0
        row["projection_unit"] = PROJECTION_UNIT[market_id]
        row["projection_source"] = "model"
        return
    sigma = market_sigma(str(row.get("league_id") or ""), market_id)
    p, low, high = side_probability(over_probability(market_id, mean, float(line), sigma), side)
    forecast["p_estimate"] = p
    forecast["confidence_interval"] = (low, high)
    _refresh_ev(forecast, row.get("book_price"))
    row["forecast"] = forecast
    row["model_lean"] = model_lean(p)
    row["projection"] = round(mean, 1)
    row["projection_games"] = n
    row["projection_unit"] = PROJECTION_UNIT[market_id]
    row["projection_source"] = source


def market_sigma(league_id: str, market_id: str) -> float:
    return _sigmas().get((league_id, market_id), FALLBACK_SIGMA.get(market_id, 28.0))


def _refresh_ev(forecast: dict[str, Any], price: Any) -> None:
    if price is None:
        return
    from spe.market.odds import ev_per_dollar
    from spe.recommend.engine import american_hold_factor

    p = float(forecast["p_estimate"])
    low, high = forecast["confidence_interval"]
    ev = ev_per_dollar(p, int(price))
    width = float(high) - float(low)
    forecast["ev"] = ev
    forecast["ev_after_haircut"] = ev - 0.5 * width * american_hold_factor(int(price))


def _played(attempts, targets, carries, receptions, rec_yards, rush_yards) -> bool:
    touches = float(attempts or 0) + float(targets or 0) + float(carries or 0) + float(receptions or 0)
    return touches > 0 or float(rec_yards or 0) != 0 or float(rush_yards or 0) != 0


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 0.0 if x < mu else 1.0
    z = (x - mu) / (sigma * math.sqrt(2.0))
    return float(0.5 * (1.0 + math.erf(z)))


@lru_cache(maxsize=1)
def _sigmas() -> dict[tuple[str, str], float]:
    from spe.models.train import load_artifacts

    found: dict[tuple[str, str], float] = {}
    for league in ("nfl", "fbs"):
        for market_id, fitted in load_artifacts(league).items():
            sigma = float(getattr(fitted, "sigma", 0) or 0)
            if sigma > 0:
                found[(league, market_id)] = sigma
    return found
