"""What historical data is available vs what requires a key, paid plan, or license.

Do not invent missing stats or sportsbook prices.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Asset:
    asset: str
    leagues: tuple[str, ...]
    availability: str  # available | needs_key | paid_plan | requires_contract | not_in_v1
    provider: str
    notes: str


ASSETS: tuple[Asset, ...] = (
    Asset(
        "nfl_schedules_1999_present",
        ("nfl",),
        "available",
        "nflverse",
        "CC-BY games.parquet. Includes closing spread/total/moneylines on many games; not player props.",
    ),
    Asset(
        "nfl_players_rosters_injuries",
        ("nfl",),
        "available",
        "nflverse",
        "players.parquet, roster_{season}.parquet, injuries_{season}.parquet.",
    ),
    Asset(
        "nfl_player_week_stats",
        ("nfl",),
        "available",
        "nflverse",
        "targets, receptions, yards, TDs, target_share, EPA. No route participation or RZ splits.",
    ),
    Asset(
        "nfl_play_by_play_epa",
        ("nfl",),
        "available",
        "nflverse",
        "play_by_play_{season}.parquet. Optional ingest (large). Do not include FTN/CC-BY-SA columns.",
    ),
    Asset(
        "nfl_observed_game_weather",
        ("nfl",),
        "available",
        "nflverse",
        "temp/wind/roof on the schedule are observed/gamebook values, NOT pregame forecasts. Using them as if known before kickoff is leakage.",
    ),
    Asset(
        "fbs_games_teams_rosters_plays_ppa",
        ("fbs",),
        "needs_key",
        "cfbd",
        "Requires CFBD_API_KEY. Tier 2 (~$5/mo, 30k calls) can cover multi-season games + weekly plays if budgeted.",
    ),
    Asset(
        "historical_player_prop_odds",
        ("nfl", "fbs"),
        "paid_plan",
        "odds_api",
        "GET /v4/historical/sports/{sport}/events/{id}/odds is paid-only. Player-prop history starts 2023-05-03. Do not fabricate ticks.",
    ),
    Asset(
        "live_player_prop_odds",
        ("nfl", "fbs"),
        "needs_key",
        "odds_api",
        "Current event odds for anytime TD, receptions, receiving yards, rushing yards. "
        "Events list is free; event odds cost 4 credits (4 markets × 1 US region). Historical ticks are not this feed.",
    ),
    Asset(
        "live_stadium_forecast_alerts",
        ("nfl", "fbs"),
        "available",
        "nws",
        "Free. No key. Hourly temperature, wind, gust, precipitation chance, and active alerts "
        "at stadium coordinates. US point grid only. Not a historical forecast archive.",
    ),
    Asset(
        "nfl_participation_plays",
        ("nfl",),
        "available",
        "nflverse",
        "pbp_participation_{season}.parquet, 2016-2025. Formation, raw personnel text, and player-id lists. Play-level route is not assigned to a player.",
    ),
    Asset(
        "nfl_ngs_summaries",
        ("nfl",),
        "available",
        "nflverse",
        "ngs_passing/rushing/receiving.parquet from 2016. Week 0 is a season aggregate and is not a pregame feature. Not tracking coordinates.",
    ),
    Asset(
        "nfl_weekly_rosters_depth_charts",
        ("nfl",),
        "available",
        "nflverse",
        "roster_weekly and depth_charts. No publication timestamp, so the chart becomes usable only after that week's last kickoff.",
    ),
    Asset(
        "historical_pregame_forecast_weather",
        ("nfl", "fbs"),
        "available_noncommercial",
        "open_meteo",
        "Previous-day1 forecasts from about 2024-01-01. Free tier is CC BY 4.0 and noncommercial. Commercial use needs a paid plan. Not an observation.",
    ),
    Asset(
        "noaa_cdo_daily_observations",
        ("nfl", "fbs"),
        "needs_key",
        "noaa_cdo",
        "Free NOAA_CDO_TOKEN. Daily GHCND observations for research. Not a kickoff forecast.",
    ),
    Asset(
        "gdelt_news_metadata",
        ("nfl", "fbs"),
        "available",
        "gdelt",
        "DOC 2.0 article list with seendate. Unverified. Not an injury report and not linked to players.",
    ),
    Asset(
        "api_sports_football",
        ("soccer",),
        "needs_key",
        "api_sports",
        "API_SPORTS_KEY. API-Football leagues and fixtures. "
        "The same key also reaches API-American-Football.",
    ),
    Asset(
        "route_participation_charting",
        ("nfl", "fbs"),
        "requires_contract",
        "pff_or_sis",
        "Not in nflverse CC-BY core or CFBD standard plays. Do not invent routes.",
    ),
    Asset(
        "coverage_assignments_wr_cb",
        ("nfl", "fbs"),
        "requires_contract",
        "pff_or_sis",
        "Requires licensed charting.",
    ),
    Asset(
        "nfl_tracking_ngs",
        ("nfl",),
        "requires_contract",
        "sportradar_or_nfl",
        "Explicitly out of lean MVP. Do not scrape or reuse Big Data Bowl files beyond their terms.",
    ),
    Asset(
        "end_zone_and_inside_five_splits",
        ("nfl", "fbs"),
        "not_in_v1",
        "derived_pbp",
        "Can be derived later from play-by-play; weekly box scores do not include them. Leave missing until PBP derivation ships.",
    ),
)


def catalog_payload() -> list[dict]:
    return [asdict(a) for a in ASSETS]
