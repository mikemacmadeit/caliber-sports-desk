from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    app_name: str = "Caliber"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    web_origin: str = "http://localhost:3000"
    database_url: str = "sqlite:///./data/caliber.db"
    parquet_dir: Path = Path("./data/parquet")

    cfbd_api_key: str = ""
    cfbd_session_call_budget: int = 280
    odds_api_key: str = ""
    # Soft local ceiling. The Odds API remaining credits still gate hard stops.
    # 400 was starving near-kickoff refreshes while the provider still had ~20k left.
    odds_credit_budget_per_day: int = 2000
    # Approved temporary soft ceiling. Active only while now < until (UTC).
    # Raised from 2312 on 2026-11-07 so a paid Odds API balance can refresh this slate.
    # At/after expiry the standard odds_credit_budget_per_day applies; never auto-extends.
    odds_credit_budget_temporary: int = 3112
    odds_credit_budget_temporary_until: str = "2026-11-08T00:00:00+00:00"
    odds_regions: str = "us"
    odds_bookmakers: str = "draftkings,fanduel,betmgm,fanatics"
    odds_markets: str = (
        "player_anytime_td,player_receptions,player_reception_yds,player_rush_yds"
    )
    odds_max_events_per_run: int = 40
    odds_min_refresh_minutes: int = 20
    # Inside two hours of kickoff, poll every few minutes and treat older prices as stale.
    odds_near_poll_minutes: int = 5
    odds_near_fresh_minutes: int = 12
    odds_upcoming_hours: int = 192
    # Far-out slate uses a thinner market set to save credits.
    odds_far_markets: str = "player_anytime_td,player_rush_yds"
    odds_near_hours: int = 12
    odds_mid_hours: int = 48
    # Keep this many soft-budget credits for near-kickoff / stale refreshes.
    odds_near_reserve_credits: int = 400
    game_complete_hours: float = 4.5

    nws_user_agent: str = "Caliber Private Research (dev@localhost)"
    open_meteo_enabled: bool = False
    noaa_cdo_token: str = ""
    api_sports_key: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    openai_input_usd_per_mtok: float = 0.40
    openai_output_usd_per_mtok: float = 1.60
    gdelt_lookback_days: int = 14

    ingest_odds_interval_sec: int = 300
    ingest_injury_interval_sec: int = 600
    # NFL inactive lists are the availability report inside this many hours of kickoff.
    inactive_window_hours: float = 2.5
    ingest_weather_interval_sec: int = 7200

    min_ev_after_haircut: float = 0.04
    max_price_age_minutes: int = 45
    min_availability_prob: float = 0.75
    disclaimer: str = (
        "Research forecast only. Not betting advice. "
        "No wagering and no automated placement in this product."
    )

    @property
    def odds_market_list(self) -> list[str]:
        return [m.strip() for m in self.odds_markets.split(",") if m.strip()]

    @property
    def odds_far_market_list(self) -> list[str]:
        return [m.strip() for m in self.odds_far_markets.split(",") if m.strip()]

    @property
    def odds_bookmaker_list(self) -> list[str]:
        return [b.strip() for b in self.odds_bookmakers.split(",") if b.strip()]


settings = Settings()
