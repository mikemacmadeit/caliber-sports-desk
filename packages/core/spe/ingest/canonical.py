"""Stable canonical IDs. Never collide seed demo IDs (kc, osu) with provider IDs."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

NFL_TEAM_NAMES: dict[str, str] = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "JAC": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAR": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "OAK": "Oakland Raiders",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SD": "San Diego Chargers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "STL": "St. Louis Rams",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington",
    "WSH": "Washington",
}


def nfl_team_id(abbr: str) -> str:
    return f"nfl:{str(abbr).strip().upper()}"


def nfl_player_id(gsis_id: str) -> str:
    return f"nfl:{gsis_id}"


def nfl_game_id(provider_game_id: str) -> str:
    return f"nfl:{provider_game_id}"


def nfl_season_id(year: int) -> str:
    return f"nfl-{int(year)}"


def nfl_venue_id(stadium: str | None) -> str:
    slug = _slug(stadium or "unknown-nfl-venue")
    return f"nfl-venue:{slug}"


def fbs_team_id(cfbd_id: str | int) -> str:
    return f"fbs:{cfbd_id}"


def fbs_player_id(cfbd_id: str | int) -> str:
    return f"fbs:{cfbd_id}"


def fbs_game_id(cfbd_id: str | int) -> str:
    return f"fbs:{cfbd_id}"


def fbs_season_id(year: int) -> str:
    return f"fbs-{int(year)}"


def fbs_venue_id(venue: str | None) -> str:
    slug = _slug(venue or "unknown-fbs-venue")
    return f"fbs-venue:{slug}"


def position_group(position: str | None) -> str:
    p = (position or "UNK").upper().strip()
    mapping = {
        "QB": "QB",
        "RB": "RB",
        "FB": "RB",
        "HB": "RB",
        "WR": "WR",
        "TE": "TE",
        "T": "OL",
        "G": "OL",
        "C": "OL",
        "OT": "OL",
        "OG": "OL",
        "OL": "OL",
        "DE": "DL",
        "DT": "DL",
        "NT": "DL",
        "DL": "DL",
        "LB": "LB",
        "ILB": "LB",
        "OLB": "LB",
        "MLB": "LB",
        "CB": "DB",
        "S": "DB",
        "FS": "DB",
        "SS": "DB",
        "DB": "DB",
        "SAF": "DB",
        "K": "K",
        "PK": "K",
        "P": "P",
        "LS": "LS",
    }
    return mapping.get(p, "UNK")


def parse_kickoff(gameday: object, gametime: object) -> datetime | None:
    if gameday is None or str(gameday) in {"", "nan", "NaT", "None"}:
        return None
    day = str(gameday)[:10]
    stamp = "18:00:00"
    if gametime is not None and str(gametime) not in {"", "nan", "None"}:
        raw = str(gametime).strip()
        if len(raw) == 5:
            stamp = f"{raw}:00"
        elif len(raw) >= 8:
            stamp = raw[:8]
    try:
        local = datetime.fromisoformat(f"{day}T{stamp}")
    except ValueError:
        try:
            local = datetime.fromisoformat(f"{day}T18:00:00")
            return local.replace(tzinfo=UTC)
        except ValueError:
            return None
    # nflverse prints Eastern wall time. A missing gametime stays the 18:00 UTC placeholder.
    if gametime is None or str(gametime) in {"", "nan", "None"}:
        return local.replace(tzinfo=UTC)
    return local.replace(tzinfo=EASTERN).astimezone(UTC)


def f_to_c(temp_f: float | None) -> float | None:
    if temp_f is None:
        return None
    return (float(temp_f) - 32.0) * 5.0 / 9.0


def mph_to_mps(wind_mph: float | None) -> float | None:
    if wind_mph is None:
        return None
    return float(wind_mph) * 0.44704


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "unknown"
