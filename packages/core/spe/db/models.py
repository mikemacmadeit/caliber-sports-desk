from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from spe.db.base import Base, ProvenanceMixin, new_id, utcnow


class Sport(Base):
    __tablename__ = "sports"
    sport_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class League(Base):
    __tablename__ = "leagues"
    league_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    sport_id: Mapped[str] = mapped_column(ForeignKey("sports.sport_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    season_type: Mapped[str] = mapped_column(String(32), nullable=False, default="regular")
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    # nfl | fbs — models NEVER share calibrated parameters across leagues


class Season(Base):
    __tablename__ = "seasons"
    season_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.league_id"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Team(Base):
    __tablename__ = "teams"
    team_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.league_id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    abbreviation: Mapped[str] = mapped_column(String(16), nullable=False)
    conference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aliases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class Player(Base):
    __tablename__ = "players"
    player_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    birth_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    position_group: Mapped[str] = mapped_column(String(16), nullable=False)
    position: Mapped[str] = mapped_column(String(16), nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class ProviderIdMap(Base):
    __tablename__ = "provider_id_map"
    map_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(160), nullable=False)
    canonical_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="auto")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("provider", "entity_type", "external_id", name="uq_provider_entity"),
        Index("ix_provider_canonical", "entity_type", "canonical_id"),
    )


class Venue(Base):
    __tablename__ = "venues"
    venue_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    altitude_m: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    roof_type: Mapped[str] = mapped_column(String(32), nullable=False)  # open|retractable|dome
    surface: Mapped[str] = mapped_column(String(32), nullable=False)
    orientation_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")


class Game(Base):
    __tablename__ = "games"
    game_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    season_id: Mapped[str] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)
    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.league_id"), nullable=False, index=True)
    home_team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    away_team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    kickoff_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venues.venue_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    neutral_site: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    data_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="demo")
    # demo | historical | experimental | validated
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_team = relationship("Team", foreign_keys=[home_team_id])
    away_team = relationship("Team", foreign_keys=[away_team_id])
    venue = relationship("Venue")
    season = relationship("Season", foreign_keys=[season_id])


class ClosingLine(Base, ProvenanceMixin):
    """Consensus or book closing game lines. Not fabricated; missing rows stay missing.

    For source='nflverse_consensus' point spreads, home_line stores raw nflverse
    spread_line L = expected home winning margin (positive ⇒ home favored).
    Sportsbook-style home handicap is h = -L. Do not flip on ingest.
    Live Odds-API outcomes.line values are already sportsbook handicaps.
    """

    __tablename__ = "closing_lines"
    line_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # point_spread|total
    home_line: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_line: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_price_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_price_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("game_id", "market_id", "source", "provider", name="uq_closing_line"),
    )


class AtsPrediction(Base):
    """Append-only ATS research prediction frozen before kickoff. Never rewritten."""

    __tablename__ = "ats_predictions"
    ats_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    league_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    home_team_id: Mapped[str] = mapped_column(String(64), nullable=False)
    away_team_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bookmaker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_id: Mapped[str] = mapped_column(String(64), nullable=False, default="point_spread")
    # Independent game-side evaluated: home or away (never both for one snapshot).
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    spread_line: Mapped[float] = mapped_column(Float, nullable=False)
    price_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odds_tick_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    predicted_margin: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_home_score: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_away_score: Mapped[float] = mapped_column(Float, nullable=False)
    p_cover: Mapped[float] = mapped_column(Float, nullable=False)
    p_push: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    margin_sigma: Mapped[float] = mapped_column(Float, nullable=False)
    model_vs_market: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    data_quality_status: Mapped[str] = mapped_column(String(32), nullable=False, default="research")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    warnings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    components: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    research_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    play_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        Index("ix_ats_dedupe", "game_id", "bookmaker_id", "side", "spread_line", "model_version_id", "odds_tick_id"),
    )


class AtsSettlement(Base):
    """One settlement per ATS prediction. Original prediction rows are never modified."""

    __tablename__ = "ats_settlements"
    settlement_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    ats_id: Mapped[str] = mapped_column(ForeignKey("ats_predictions.ats_id"), nullable=False, unique=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)  # cover|loss|push|void
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    official_source: Mapped[str] = mapped_column(String(128), nullable=False)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    flat_stake_return: Mapped[float | None] = mapped_column(Float, nullable=True)


class Roster(Base):
    __tablename__ = "rosters"
    roster_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False, index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    jersey: Mapped[str | None] = mapped_column(String(8), nullable=True)


class Play(Base, ProvenanceMixin):
    __tablename__ = "plays"
    play_pk: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    play_id: Mapped[str] = mapped_column(String(64), nullable=False)
    period: Mapped[int] = mapped_column(Integer, nullable=False)
    clock_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    down: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance: Mapped[float | None] = mapped_column(Float, nullable=True)
    yardline: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offense_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    defense_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    play_type: Mapped[str] = mapped_column(String(64), nullable=False)
    yards: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    epa: Mapped[float | None] = mapped_column(Float, nullable=True)
    ppa: Mapped[float | None] = mapped_column(Float, nullable=True)
    wp: Mapped[float | None] = mapped_column(Float, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_pass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_rush: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_td: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_turnover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_red_zone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_goal_line: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_scrimmage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_down: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    game_seconds_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drive_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    passer_player_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rusher_player_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receiver_player_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score_before_off: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score_before_def: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("game_id", "play_id", "provider", name="uq_play"),)


class PlayerPlayParticipation(Base, ProvenanceMixin):
    __tablename__ = "player_play_participation"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False)
    play_id: Mapped[str] = mapped_column(String(64), nullable=False)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    route: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    carry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    snap: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    coverage_assignment: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PlayerGameStat(Base, ProvenanceMixin):
    __tablename__ = "player_game_stats"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    routes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    targets: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    carries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    receptions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rush_yards: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    rec_yards: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    rush_td: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rec_td: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pass_td: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rz_targets: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rz_carries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gl_carries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    team_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    __table_args__ = (UniqueConstraint("game_id", "player_id", "provider", "revision", name="uq_pgs"),)


class TeamGameStat(Base, ProvenanceMixin):
    __tablename__ = "team_game_stats"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    epa_per_play: Mapped[float | None] = mapped_column(Float, nullable=True)
    success_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    pace_sec_per_play: Mapped[float | None] = mapped_column(Float, nullable=True)
    explosiveness: Mapped[float | None] = mapped_column(Float, nullable=True)
    red_zone_td_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    implied_total: Mapped[float | None] = mapped_column(Float, nullable=True)


class InjuryEvent(Base, ProvenanceMixin):
    __tablename__ = "injury_events"
    injury_event_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str | None] = mapped_column(ForeignKey("games.game_id"), nullable=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False, index=True)
    designation: Mapped[str] = mapped_column(String(32), nullable=False)
    practice_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # Missing report MUST NOT be treated as healthy.
    coverage_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DepthChartSnapshot(Base, ProvenanceMixin):
    __tablename__ = "depth_chart_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False)
    position: Mapped[str] = mapped_column(String(16), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ParticipationPlay(Base, ProvenanceMixin):
    """One nflverse participation row per play. Route text is play-level, not a player chart."""

    __tablename__ = "participation_plays"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    play_id: Mapped[str] = mapped_column(String(32), nullable=False)
    possession_team: Mapped[str | None] = mapped_column(String(8), nullable=True)
    offense_formation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    offense_personnel: Mapped[str | None] = mapped_column(Text, nullable=True)
    defense_personnel: Mapped[str | None] = mapped_column(Text, nullable=True)
    defenders_in_box: Mapped[float | None] = mapped_column(Float, nullable=True)
    number_of_pass_rushers: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_offense: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_defense: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route: Mapped[str | None] = mapped_column(String(64), nullable=True)
    defense_man_zone_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    defense_coverage_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    time_to_throw: Mapped[float | None] = mapped_column(Float, nullable=True)
    was_pressure: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ngs_air_yards: Mapped[float | None] = mapped_column(Float, nullable=True)
    offense_players: Mapped[str | None] = mapped_column(Text, nullable=True)
    defense_players: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("game_id", "play_id", "provider", name="uq_participation_play"),)


class ParticipationPlayerGame(Base, ProvenanceMixin):
    """Snap counts derived by splitting the play-level player-id lists. Not charted routes."""

    __tablename__ = "participation_player_games"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False, index=True)
    offense_snaps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    defense_snaps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    offense_plays: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    defense_plays: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (
        UniqueConstraint("game_id", "player_id", "provider", name="uq_participation_player_game"),
    )


class NgsWeek(Base, ProvenanceMixin):
    __tablename__ = "ngs_weeks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    season_type: Mapped[str] = mapped_column(String(8), nullable=False, default="REG")
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False, index=True)
    team_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stat_type: Mapped[str] = mapped_column(String(16), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    __table_args__ = (
        UniqueConstraint(
            "season_year", "week", "season_type", "player_id", "stat_type", "provider",
            name="uq_ngs_week",
        ),
    )


class RosterWeek(Base, ProvenanceMixin):
    __tablename__ = "roster_weeks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    game_type: Mapped[str] = mapped_column(String(8), nullable=False, default="REG")
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False, index=True)
    position: Mapped[str | None] = mapped_column(String(16), nullable=True)
    depth_chart_position: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    jersey: Mapped[str | None] = mapped_column(String(8), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "season_year", "week", "game_type", "team_id", "player_id", "provider",
            name="uq_roster_week",
        ),
    )


class DepthChartWeek(Base, ProvenanceMixin):
    __tablename__ = "depth_chart_weeks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    game_type: Mapped[str] = mapped_column(String(8), nullable=False, default="REG")
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False, index=True)
    formation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position: Mapped[str | None] = mapped_column(String(16), nullable=True)
    depth_position: Mapped[str | None] = mapped_column(String(16), nullable=True)
    depth_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "season_year",
            "week",
            "game_type",
            "team_id",
            "player_id",
            "formation",
            "depth_position",
            "provider",
            name="uq_depth_week",
        ),
    )


class NewsArticle(Base, ProvenanceMixin):
    """A news hit. This is not an injury designation and must not be graded as one."""

    __tablename__ = "news_articles"
    article_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    theme: Mapped[str] = mapped_column(String(32), nullable=False)
    league_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verified_injury: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class WeatherSnapshot(Base, ProvenanceMixin):
    __tablename__ = "weather_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venues.venue_id"), nullable=False, index=True)
    game_id: Mapped[str | None] = mapped_column(ForeignKey("games.game_id"), nullable=True)
    forecast_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    gust_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_dir_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    precip_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    precip_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(String(160), nullable=True)
    alerts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    roof_status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")


class Market(Base):
    __tablename__ = "markets"
    market_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sport: Mapped[str] = mapped_column(String(32), nullable=False, default="football")
    market_type: Mapped[str] = mapped_column(String(64), nullable=False)
    period: Mapped[str] = mapped_column(String(32), nullable=False, default="game")
    stat: Mapped[str] = mapped_column(String(64), nullable=False)
    grading_rule: Mapped[str] = mapped_column(Text, nullable=False)
    odds_api_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Outcome(Base):
    __tablename__ = "outcomes"
    outcome_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.market_id"), nullable=False, index=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.player_id"), nullable=True, index=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)  # yes|no|over|under
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(String(256), nullable=False, default="")


class Bookmaker(Base):
    __tablename__ = "bookmakers"
    bookmaker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    region: Mapped[str] = mapped_column(String(16), nullable=False, default="us")


class OddsTick(Base, ProvenanceMixin):
    __tablename__ = "odds_ticks"
    tick_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcomes.outcome_id"), nullable=False, index=True)
    bookmaker_id: Mapped[str] = mapped_column(ForeignKey("bookmakers.bookmaker_id"), nullable=False)
    price_american: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    limits: Mapped[float | None] = mapped_column(Float, nullable=True)
    credit_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class FeatureRow(Base):
    __tablename__ = "features"
    feature_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.player_id"), nullable=True)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    feature_blob: Mapped[dict] = mapped_column(JSON, nullable=False)
    coverage_mask: Mapped[dict] = mapped_column(JSON, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missingness: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(64), nullable=False)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    model_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    training_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_snapshot: Mapped[str] = mapped_column(String(128), nullable=False)
    hyperparameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    calibration_version: Mapped[str] = mapped_column(String(32), nullable=False)
    league_id: Mapped[str] = mapped_column(String(16), nullable=False)
    family: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Forecast(Base):
    __tablename__ = "forecasts"
    forecast_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcomes.outcome_id"), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.model_version_id"), nullable=False
    )
    p: Mapped[float] = mapped_column(Float, nullable=False)
    p_low: Mapped[float] = mapped_column(Float, nullable=False)
    p_high: Mapped[float] = mapped_column(Float, nullable=False)
    sim_count: Mapped[int] = mapped_column(Integer, nullable=False)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    availability_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    components: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="demo")


class SimulationRun(Base):
    __tablename__ = "simulation_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    draw_count: Mapped[int] = mapped_column(Integer, nullable=False)
    assumptions: Mapped[dict] = mapped_column(JSON, nullable=False)
    scenario: Mapped[str] = mapped_column(String(32), nullable=False, default="official")
    overrides: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.model_version_id"), nullable=False
    )
    parquet_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    convergence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    research_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Recommendation(Base):
    __tablename__ = "recommendations"
    recommendation_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.forecast_id"), nullable=False)
    odds_tick_id: Mapped[str | None] = mapped_column(ForeignKey("odds_ticks.tick_id"), nullable=True)
    ev: Mapped[float | None] = mapped_column(Float, nullable=True)
    ev_after_haircut: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # play|no_bet|void
    reason_codes: Mapped[list] = mapped_column(JSON, nullable=False)
    kelly_fractional: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="demo")


class Settlement(Base):
    __tablename__ = "settlements"
    settlement_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcomes.outcome_id"), nullable=False, unique=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)  # win|loss|push|void
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    official_source: Mapped[str] = mapped_column(String(128), nullable=False)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class RawSnapshot(Base):
    """Append-only raw provider payload. Never mutate; new rows are revisions."""

    __tablename__ = "raw_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_record_id: Mapped[str] = mapped_column(String(160), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    license_name: Mapped[str] = mapped_column(String(128), nullable=False)
    credit_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CreditUsage(Base):
    __tablename__ = "credit_usage"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    credits_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("provider", "day", name="uq_credit_day"),)


class ModelHealthSnapshot(Base):
    __tablename__ = "model_health_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    league_id: Mapped[str] = mapped_column(String(16), nullable=False)
    market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    brier: Mapped[float] = mapped_column(Float, nullable=False)
    log_loss: Mapped[float] = mapped_column(Float, nullable=False)
    ece: Mapped[float] = mapped_column(Float, nullable=False)
    skill_vs_novig: Mapped[float | None] = mapped_column(Float, nullable=True)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage: Mapped[float] = mapped_column(Float, nullable=False)
    freshness_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    clv: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class IngestRun(Base):
    __tablename__ = "ingest_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    league_id: Mapped[str] = mapped_column(String(16), nullable=False)
    season_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rows_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DataCoverage(Base):
    __tablename__ = "data_coverage"
    coverage_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    league_id: Mapped[str] = mapped_column(String(16), nullable=False)
    season_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    __table_args__ = (
        UniqueConstraint("provider", "dataset", "league_id", "season_year", name="uq_coverage"),
    )


class TeamAnalytics(Base):
    """Team metrics at game, week, or season grain. JSON is calculated, never invented."""

    __tablename__ = "team_analytics"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    league_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False, index=True)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    game_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    grain: Mapped[str] = mapped_column(String(16), nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False, default="all")
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    coverage: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "league_id", "team_id", "grain", "split", "season_year", "week", "game_id",
            name="uq_team_analytics",
        ),
    )


class PlayerAnalytics(Base):
    __tablename__ = "player_analytics"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    league_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False, index=True)
    team_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    game_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    grain: Mapped[str] = mapped_column(String(16), nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False, default="all")
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    coverage: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "league_id", "player_id", "grain", "split", "season_year", "week", "game_id",
            name="uq_player_analytics",
        ),
    )


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    league_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    train_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    test_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    test_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    brier: Mapped[float | None] = mapped_column(Float, nullable=True)
    log_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    ece: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AiRun(Base):
    """One cached model call. Official probabilities are never stored here."""

    __tablename__ = "ai_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    game_id: Mapped[str | None] = mapped_column(ForeignKey("games.game_id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    token_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    rejection_reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    output: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    data_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="demo")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint("kind", "input_hash", name="uq_ai_run_hash"),)


class AiFinding(Base):
    __tablename__ = "ai_findings"
    finding_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_runs.run_id"), nullable=False, index=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(32), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(16), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    subject_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    stance: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    affected_player_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AiSignal(Base):
    """A research signal. It does not write model inputs unless Python validates a number."""

    __tablename__ = "ai_signals"
    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_runs.run_id"), nullable=False, index=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    magnitude: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(16), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unverified")
    scenario_status: Mapped[str] = mapped_column(String(32), nullable=False, default="speculative")
    simulation_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    numeric_override: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AiOpportunity(Base):
    """A researched market opportunity. Never stores an invented edge or AI probability."""

    __tablename__ = "ai_opportunities"
    opportunity_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_runs.run_id"), nullable=False, index=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    claim_type: Mapped[str] = mapped_column(String(16), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    missing_information: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    recommended_analysis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unverified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AiAlert(Base):
    __tablename__ = "ai_alerts"
    alert_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_runs.run_id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    affected_forecast_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    model_response: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AiExplanation(Base):
    __tablename__ = "ai_explanations"
    explanation_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("ai_runs.run_id"), nullable=False, index=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False, index=True)
    forecast_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    recommendation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    narrative: Mapped[str] = mapped_column(Text, nullable=False, default="")
    factors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    assumptions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    uncertainty: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AiReview(Base):
    """A suggested experiment. applied stays false; nothing here edits the model."""

    __tablename__ = "ai_reviews"
    review_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    forecast_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    suggested_experiment: Mapped[str] = mapped_column(Text, nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketComparison(Base):
    """A settled sportsbook prop beside the forecast that was on record. Research lines are not inserted here."""

    __tablename__ = "market_comparisons"
    comparison_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    player_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    game_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bookmaker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_american: Mapped[int] = mapped_column(Integer, nullable=False)
    forecast_probability: Mapped[float] = mapped_column(Float, nullable=False)
    forecast_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actual_outcome: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ShadowPrediction(Base):
    """Append-only prospective forecast at a captured sportsbook price. Never rewritten."""

    __tablename__ = "shadow_predictions"
    shadow_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    player_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    game_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    league_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    outcome_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    bookmaker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    price_american: Mapped[int] = mapped_column(Integer, nullable=False)
    odds_tick_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    forecast_probability: Mapped[float] = mapped_column(Float, nullable=False)
    market_implied_raw: Mapped[float] = mapped_column(Float, nullable=False)
    market_implied_novig: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    forecast_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    data_quality_status: Mapped[str] = mapped_column(String(32), nullable=False)
    abstain_reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    recommendation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="no_bet")
    components: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    research_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    play_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        Index("ix_shadow_dedupe", "outcome_id", "model_version_id", "odds_tick_id"),
    )


class ShadowSettlement(Base):
    """One settlement per shadow prediction. Missing stats are not zeros."""

    __tablename__ = "shadow_settlements"
    settlement_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    shadow_id: Mapped[str] = mapped_column(ForeignKey("shadow_predictions.shadow_id"), nullable=False, unique=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)  # win|loss|push|void|blocked
    actual_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    official_source: Mapped[str] = mapped_column(String(128), nullable=False)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    flat_stake_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    closing_price_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closing_line_value: Mapped[float | None] = mapped_column(Float, nullable=True)


class JobRun(Base):
    """Scheduled shadow-job ledger. Safe to restart; failures stay visible."""

    __tablename__ = "job_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BacktestPrediction(Base):
    __tablename__ = "backtest_predictions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.run_id"), nullable=False, index=True)
    game_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    player_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    train_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    p: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    components: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
