"""Time-safe test: prior-week poll and preseason talent on college ATS.

The selection season is 2023. The holdout from 2024-08-15 is scored once.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from spe.adapters.cfbd import CfbdAdapter
from spe.db.models import Team
from spe.db.session import init_db, session_factory
from spe.models.ats.model import fit_ridge_margin, score_predictions
from spe.models.ats.ratings import FEATURE_COLS, build_team_strength_frame
from spe.models.ats.spread_contract import attach_spread_fields

YEARS = range(2016, 2026)
UNRANKED = 40.0


def _poll_ranks(week_row: dict) -> dict[str, float]:
    polls = {p.get("poll"): p.get("ranks") or [] for p in (week_row.get("polls") or [])}
    ranks = polls.get("AP Top 25") or polls.get("Coaches Poll") or []
    out = {}
    for row in ranks:
        team_id = row.get("teamId")
        rank = row.get("rank")
        if team_id is None or rank is None:
            continue
        out[f"fbs:{team_id}"] = float(rank)
    return out


def load_rank_history(adapter: CfbdAdapter) -> dict[tuple[int, int], dict[str, float]]:
    """Map (season, week) -> team_id -> rank. Week is the poll week, used only after it is published."""
    history: dict[tuple[int, int], dict[str, float]] = {}
    for year in YEARS:
        fetched = adapter.fetch("rankings", {"year": year, "seasonType": "regular"})
        for row in fetched.records:
            week = row.get("week")
            if week is None:
                continue
            history[(int(year), int(week))] = _poll_ranks(row)
        print(f"rankings {year} weeks={sum(1 for y, _w in history if y == year)}", flush=True)
    return history


def school_index(session) -> dict[str, str]:
    """CFBD school name -> team_id. Exact name wins over a longer nickname."""
    exact: dict[str, str] = {}
    prefix: dict[str, str] = {}
    for team in session.query(Team).filter(Team.league_id == "fbs").all():
        exact[team.name.lower()] = team.team_id
        prefix[team.name.lower()] = team.team_id
    return {"exact": exact, "teams": [(t.name.lower(), t.team_id) for t in session.query(Team).filter(Team.league_id == "fbs")]}


def resolve_school(index: dict, school: str) -> str | None:
    key = school.lower().strip()
    if key in index["exact"]:
        return index["exact"][key]
    hits = [team_id for name, team_id in index["teams"] if name.startswith(key + " ")]
    if len(hits) == 1:
        return hits[0]
    return None


def load_talent(adapter: CfbdAdapter, index: dict) -> dict[tuple[int, str], float]:
    talent: dict[tuple[int, str], float] = {}
    for year in YEARS:
        fetched = adapter.fetch("talent", {"year": year})
        matched = 0
        for row in fetched.records:
            team_id = resolve_school(index, str(row.get("team") or ""))
            value = row.get("talent")
            if team_id is None or value is None:
                continue
            talent[(int(year), team_id)] = float(value)
            matched += 1
        print(f"talent {year} matched={matched} of {len(fetched.records)}", flush=True)
    return talent


def prior_rank(history: dict, season: int, week: int, team_id: str) -> float:
    for prev in range(int(week) - 1, 0, -1):
        ranks = history.get((int(season), prev))
        if not ranks:
            continue
        return float(ranks.get(str(team_id), UNRANKED))
    return UNRANKED


def cover(scored: pd.DataFrame) -> tuple[float, int]:
    edge = scored["predicted_margin"].to_numpy() - scored["market_home_margin"].to_numpy()
    actual = scored["margin"].to_numpy() - scored["market_home_margin"].to_numpy()
    mask = np.abs(actual) > 1e-6
    hit = np.sign(edge[mask]) == np.sign(actual[mask])
    return float(hit.mean()), int(mask.sum())


def main() -> None:
    adapter = CfbdAdapter()
    init_db()
    session = session_factory()
    talent = load_talent(adapter, school_index(session))
    history = load_rank_history(adapter)
    frame = attach_spread_fields(build_team_strength_frame(session, "fbs"))
    session.close()
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
    home_rank = []
    away_rank = []
    home_talent = []
    away_talent = []
    for row in frame.itertuples(index=False):
        season = int(row.season_year)
        week = int(row.week or 0)
        home_rank.append(prior_rank(history, season, week, row.home_team_id))
        away_rank.append(prior_rank(history, season, week, row.away_team_id))
        home_talent.append(talent.get((season, str(row.home_team_id)), np.nan))
        away_talent.append(talent.get((season, str(row.away_team_id)), np.nan))
    frame["home_rank"] = home_rank
    frame["away_rank"] = away_rank
    frame["rank_edge"] = frame["away_rank"] - frame["home_rank"]
    frame["home_talent"] = home_talent
    frame["away_talent"] = away_talent
    frame["talent_edge"] = frame["home_talent"] - frame["away_talent"]
    data = frame.dropna(subset=["margin", "market_home_margin"]).copy()
    print(
        "rows",
        len(data),
        "talent_known",
        int(data["talent_edge"].notna().sum()),
        "ranked_home",
        int((data["home_rank"] < UNRANKED).sum()),
        flush=True,
    )
    data["talent_edge"] = data["talent_edge"].fillna(0.0)
    select_at = pd.Timestamp("2023-08-15", tz="UTC")
    hold_at = pd.Timestamp("2024-08-15", tz="UTC")
    train = data[data["kickoff_utc"] < select_at]
    val = data[(data["kickoff_utc"] >= select_at) & (data["kickoff_utc"] < hold_at)]
    extra = ["rank_edge", "talent_edge"]
    base = list(FEATURE_COLS)
    results = {}
    for name, cols in (("base", base), ("rank_talent", base + extra)):
        art = fit_ridge_margin(train, league_id="fbs", alpha=12.0, feature_cols=cols)
        rate, n = cover(score_predictions(art, val, apply_calibration=False))
        results[name] = rate
        print(f"VAL {name} cover={rate:.4f} n={n}", flush=True)
    chosen = max(results, key=results.get)
    print("CHOSEN", chosen, flush=True)
    fit = data[data["kickoff_utc"] < hold_at]
    hold = data[data["kickoff_utc"] >= hold_at]
    cols = base + extra if chosen == "rank_talent" else base
    art = fit_ridge_margin(fit, league_id="fbs", alpha=12.0, feature_cols=cols)
    rate, n = cover(score_predictions(art, hold, apply_calibration=False))
    base_art = fit_ridge_margin(fit, league_id="fbs", alpha=12.0, feature_cols=base)
    base_rate, base_n = cover(score_predictions(base_art, hold, apply_calibration=False))
    print(f"HOLD chosen={chosen} cover={rate:.4f} n={n} base={base_rate:.4f} n={base_n}", flush=True)


if __name__ == "__main__":
    main()
