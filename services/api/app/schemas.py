from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DisclaimerMixin(BaseModel):
    disclaimer: str


class ForecastPayload(BaseModel):
    prediction_as_of: datetime
    model_version: str
    data_cutoff: datetime
    markets: list[str]
    p_estimate: float
    confidence_interval: tuple[float, float]
    book_price: int | None
    source_timestamp: datetime | None
    availability_status: str
    reason_codes: list[str]
    disclaimer: str
    ev: float | None = None
    ev_after_haircut: float | None = None
    recommendation_status: str | None = None


class SlateGame(BaseModel):
    game_id: str
    league_id: str
    week: int | None
    kickoff_utc: datetime
    status: str
    home: dict[str, Any]
    away: dict[str, Any]
    venue: dict[str, Any]
    weather: dict[str, Any] | None
    model_freshness_minutes: float | None
    implied_total: float | None
    conference: str | None = None


class SlateResponse(DisclaimerMixin):
    date: str
    league: str
    games: list[SlateGame]
    model_versions: dict[str, str]


class PropRow(BaseModel):
    outcome_id: str
    game_id: str
    league_id: str
    market_id: str
    player_id: str | None
    player_name: str
    team: str
    opponent: str
    kickoff_utc: datetime
    side: str
    line: float | None
    bookmaker: str | None
    book_price: int | None
    source_timestamp: datetime | None
    forecast: ForecastPayload
    best_available: bool = True


class PropsResponse(DisclaimerMixin):
    market: str
    rows: list[PropRow]


class GameDetail(DisclaimerMixin):
    game: SlateGame
    injuries: list[dict[str, Any]]
    weather: dict[str, Any] | None
    key_features: list[dict[str, Any]]
    props: list[PropRow]
    model_disagreement: list[dict[str, Any]]


class PlayerForecast(DisclaimerMixin):
    player_id: str
    name: str
    game_id: str
    distributions: dict[str, Any]
    usage: dict[str, Any]
    injury: dict[str, Any]
    forecasts: list[ForecastPayload]


class SimulationCreate(BaseModel):
    game_id: str
    as_of: datetime | None = None
    scenario_overrides: dict[str, Any] = Field(default_factory=dict)
    draw_count: int = 10_000
    seed: int = 42


class ParlayLeg(BaseModel):
    player_id: str
    market_id: str
    side: str = "yes"
    line: float | None = None


class ParlayEvaluate(BaseModel):
    game_id: str
    legs: list[ParlayLeg]
    book_american: int | None = None
    book_decimal: float | None = None
