from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ClaimType = Literal["fact", "inference", "hypothetical"]
Topic = Literal[
    "injury",
    "personnel",
    "tendency",
    "matchup",
    "trend",
    "coaching",
    "weather",
    "environment",
    "home_field",
    "scoring",
    "workload",
    "depth_chart",
]
Metric = Literal["targets", "carries", "yards", "touchdowns", "receptions", "receiving_yards", "rushing_yards"]
Direction = Literal["up", "down", "uncertain"]
Magnitude = Literal["material", "minor"]
MarketId = Literal["anytime_td", "receptions", "receiving_yards", "rushing_yards", "opportunity"]

# Markets the research desk may flag. No numeric edge is allowed on these rows.
PROP_MARKETS = frozenset(MarketId.__args__)  # type: ignore[attr-defined]


class Evidence(BaseModel):
    source: str
    ref: str
    observed_at: str | None = None


class Finding(BaseModel):
    topic: Topic
    claim_type: ClaimType
    statement: str
    confidence: float = Field(ge=0, le=1)
    affected_player_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    subject_key: str = ""
    stance: str = ""
    markets_affected: list[MarketId] = Field(default_factory=list)


class Signal(BaseModel):
    """Workload / availability direction used for research-only scenarios."""

    player_id: str
    metric: Metric
    direction: Direction
    magnitude: Magnitude
    claim_type: ClaimType
    statement: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)


class Opportunity(BaseModel):
    """A research opportunity. Never includes a numeric edge or invented probability."""

    player_id: str
    market_id: MarketId
    claim_type: ClaimType
    explanation: str
    evidence_confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    recommended_analysis: str = ""


class ResearchOutput(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    opportunities: list[Opportunity] = Field(default_factory=list)
    abstain_reason: str | None = None


class ForecastEcho(BaseModel):
    forecast_id: str
    recommendation_status: str
    reason_codes: list[str] = Field(default_factory=list)


class ExplanationOutput(BaseModel):
    factors: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    narrative: str = ""
    what_could_change: list[str] = Field(default_factory=list)
    forecasts: list[ForecastEcho] = Field(default_factory=list)


ReviewConcern = Literal[
    "assumption",
    "opportunity_estimate",
    "matchup",
    "injury_uncertainty",
    "simulation_consistency",
    "missing_information",
]


class ReviewFlag(BaseModel):
    forecast_id: str
    concern: ReviewConcern
    claim_type: ClaimType
    statement: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)
    recommended_test: str = ""


class ReviewOutput(BaseModel):
    flags: list[ReviewFlag] = Field(default_factory=list)
    abstain_reason: str | None = None
