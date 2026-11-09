"""Read-only adapter: Team TD v1 artifacts → TeamScoringContext.

Does not import training code that writes artifacts. Does not modify team_td files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import Game, Team
from spe.models.player_td.integration import TeamScoringContext, TeamScoringProvider
from spe.models.team_td.dataset import (
    build_team_td_frame,
    features_for_upcoming_game,
    injury_skill_out,
    weather_by_game,
)
from spe.models.team_td.model import predict_team_row
from spe.models.team_td.train import load_team_td_model


def _pmf_tuple(pred: dict[str, Any]) -> tuple[float, ...]:
    p = pred.get("p_off_td") or {}
    return tuple(float(p.get(k, 0.0)) for k in ("0", "1", "2", "3", "4", "5+"))


class TeamTdV1Provider:
    """Caches history frames and scores games into TeamScoringContext."""

    def __init__(self, session: Session, *, hist_cache: dict[str, pd.DataFrame] | None = None) -> None:
        self.session = session
        self._hist: dict[str, pd.DataFrame] = dict(hist_cache or {})
        self._weather: dict[str, dict[str, Any]] = {}
        self._injuries: dict[str, dict[str, dict[str, Any]]] = {}
        self._by_key: dict[tuple[str, str], TeamScoringContext] = {}
        self._raw: dict[tuple[str, str], dict[str, Any]] = {}

    def _ensure_hist(self, league_id: str) -> pd.DataFrame:
        if league_id not in self._hist:
            self._hist[league_id] = build_team_td_frame(self.session, league_id)
        if league_id not in self._weather:
            self._weather[league_id] = weather_by_game(self.session, league_id)
        if league_id not in self._injuries:
            self._injuries[league_id] = injury_skill_out(self.session, league_id)
        return self._hist[league_id]

    def score_game(self, game: Game) -> dict[str, TeamScoringContext]:
        model = load_team_td_model(game.league_id)
        if model is None:
            return {}
        hist = self._ensure_hist(game.league_id)
        if hist.empty:
            return {}
        out: dict[str, TeamScoringContext] = {}
        for team_id, is_home in ((game.home_team_id, True), (game.away_team_id, False)):
            opp_id = game.away_team_id if is_home else game.home_team_id
            feats = features_for_upcoming_game(
                hist,
                game_id=game.game_id,
                team_id=team_id,
                opp_id=opp_id,
                kickoff_utc=game.kickoff_utc,
                is_home=is_home,
                neutral_site=bool(game.neutral_site),
                league_id=game.league_id,
                weather=self._weather[game.league_id].get(game.game_id),
                injury=self._injuries[game.league_id].get(game.game_id, {}).get(team_id),
            )
            pred = predict_team_row(model, feats)
            ctx = TeamScoringContext(
                game_id=game.game_id,
                team_id=team_id,
                league_id=game.league_id,
                expected_off_tds=float(pred["lambda_off_td"]),
                expected_rush_tds=float(pred["lambda_rush_td"]),
                expected_rec_tds=float(pred["lambda_pass_td"]),
                off_td_pmf=_pmf_tuple(pred),
                train_end=model.train_end,
                model_version=f"team_td_v1-{game.league_id}",
                research_only=True,
                notes=("adapter_from_team_td_v1", "pass_td_means_receiving_tds"),
            )
            out[team_id] = ctx
            self._by_key[(game.game_id, team_id)] = ctx
            self._raw[(game.game_id, team_id)] = {
                **pred,
                "features_snapshot": {
                    "prior_off_td": feats.get("prior_off_td"),
                    "opp_prior_off_td_allowed": feats.get("opp_prior_off_td_allowed"),
                    "epa_edge": feats.get("epa_edge"),
                    "exp_possessions": feats.get("exp_possessions"),
                    "home_field": feats.get("home_field"),
                    "weather_available": feats.get("weather_available"),
                    "injury_report_complete": feats.get("injury_report_complete"),
                    "sample_size": feats.get("sample_size"),
                },
            }
        return out

    def team_scoring_for_game(self, game_id: str, team_id: str) -> TeamScoringContext | None:
        return self._by_key.get((game_id, team_id))

    def raw_prediction(self, game_id: str, team_id: str) -> dict[str, Any] | None:
        return self._raw.get((game_id, team_id))


def upcoming_games(
    session: Session,
    *,
    leagues: tuple[str, ...] = ("nfl", "fbs"),
    horizon_hours: int = 24 * 14,
    now: datetime | None = None,
    limit_per_league: int | None = 8,
) -> list[Game]:
    moment = as_utc(now) or datetime.now(UTC)
    games: list[Game] = []
    for league_id in leagues:
        q = (
            session.query(Game)
            .filter(
                Game.league_id == league_id,
                Game.data_tier == "upcoming",
                Game.status.in_(("scheduled", "preview", "pregame")),
                Game.kickoff_utc >= moment,
                Game.kickoff_utc <= moment + timedelta(hours=horizon_hours),
            )
            .order_by(Game.kickoff_utc)
        )
        rows = q.all()
        if limit_per_league is not None:
            rows = rows[:limit_per_league]
        games.extend(rows)
    return games


def team_names(session: Session, game: Game) -> dict[str, str]:
    teams = {
        t.team_id: t.name
        for t in session.query(Team).filter(Team.team_id.in_([game.home_team_id, game.away_team_id])).all()
    }
    return {
        "home": teams.get(game.home_team_id, game.home_team_id),
        "away": teams.get(game.away_team_id, game.away_team_id),
    }


# Typing helper for Protocol conformance
def as_provider(provider: TeamTdV1Provider) -> TeamScoringProvider:
    return provider  # type: ignore[return-value]
