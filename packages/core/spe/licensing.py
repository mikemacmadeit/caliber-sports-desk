"""Source license metadata. Private research ≠ commercial redistribution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Redisplay = Literal["unknown", "forbidden", "permitted", "requires_contract"]


@dataclass(frozen=True)
class LicenseMetadata:
    provider: str
    license_name: str
    license_url: str
    commercial_redisplay: Redisplay
    attribution_required: bool
    notes: str
    avoid_features: tuple[str, ...] = ()


LICENSES: dict[str, LicenseMetadata] = {
    "cfbd": LicenseMetadata(
        provider="cfbd",
        license_name="CollegeFootballData API terms / paid tier",
        license_url="https://collegefootballdata.com/api-tiers",
        commercial_redisplay="requires_contract",
        attribution_required=True,
        notes=(
            "Tier 2 (~$5/mo, 30k calls) is the lean FBS feed. "
            "Verify redistribution and commercial rights before any public launch."
        ),
    ),
    "nflverse": LicenseMetadata(
        provider="nflverse",
        license_name="CC-BY-4.0 (nflverse-data releases)",
        license_url="https://github.com/nflverse/nflverse-data",
        commercial_redisplay="requires_contract",
        attribution_required=True,
        notes=(
            "Attribute CC-BY data. Do not include FTN/CC-BY-SA-derived features "
            "in a proprietary app pending license review."
        ),
        avoid_features=("ftn_charting", "cc_by_sa_derived"),
    ),
    "odds_api": LicenseMetadata(
        provider="odds_api",
        license_name="The Odds API terms",
        license_url="https://the-odds-api.com/",
        commercial_redisplay="requires_contract",
        attribution_required=True,
        notes=(
            "Credits scale by market, region, bookmaker, and snapshot — they are not "
            "1:1 with HTTP requests. Do not redisplay bookmaker feeds without rights."
        ),
    ),
    "nws": LicenseMetadata(
        provider="nws",
        license_name="NWS / NOAA public web services",
        license_url="https://www.weather.gov/documentation/services-web-api",
        commercial_redisplay="unknown",
        attribution_required=True,
        notes="Verify terms, rate limits, User-Agent, and location coverage.",
    ),
    "open_meteo": LicenseMetadata(
        provider="open_meteo",
        license_name="Open-Meteo CC BY 4.0 (noncommercial) / paid commercial",
        license_url="https://open-meteo.com/en/pricing",
        commercial_redisplay="requires_contract",
        attribution_required=True,
        notes=(
            "Previous Runs API is on the free noncommercial tier and is CC BY 4.0. "
            "Commercial use needs a paid subscription. API Standard does not include Previous Runs."
        ),
    ),
    "noaa_cdo": LicenseMetadata(
        provider="noaa_cdo",
        license_name="NOAA Climate Data Online / public domain observations",
        license_url="https://www.ncei.noaa.gov/cdo-web/webservices/v2",
        commercial_redisplay="permitted",
        attribution_required=True,
        notes=(
            "Free token required. Five requests per second and 10,000 requests per day. "
            "Daily station observations are not kickoff forecasts."
        ),
    ),
    "gdelt": LicenseMetadata(
        provider="gdelt",
        license_name="GDELT Project DOC 2.0",
        license_url="https://www.gdeltproject.org/",
        commercial_redisplay="unknown",
        attribution_required=True,
        notes=(
            "Article metadata only. Headlines are not verified injury reports. "
            "Confirm GDELT redistribution terms before a commercial product republishes articles."
        ),
    ),
    "api_sports": LicenseMetadata(
        provider="api_sports",
        license_name="API-Sports subscription terms",
        license_url="https://www.api-football.com/documentation-v3",
        commercial_redisplay="requires_contract",
        attribution_required=True,
        notes=(
            "One key serves API-Football and API-American-Football. "
            "Do not redisplay fixtures, odds, or logos beyond the subscription terms."
        ),
    ),
    "openai": LicenseMetadata(
        provider="openai",
        license_name="OpenAI API terms",
        license_url="https://openai.com/policies/terms-of-use/",
        commercial_redisplay="requires_contract",
        attribution_required=False,
        notes=(
            "Research narrative only. Official probabilities stay in the Python engine. "
            "Do not send the API key to the browser."
        ),
    ),
    "seed": LicenseMetadata(
        provider="seed",
        license_name="Synthetic demonstration data",
        license_url="",
        commercial_redisplay="permitted",
        attribution_required=False,
        notes="Local fixture/seed data. Not a licensed live feed.",
    ),
}


def license_for(provider: str) -> LicenseMetadata:
    if provider not in LICENSES:
        raise KeyError(f"Unknown provider '{provider}'")
    return LICENSES[provider]
