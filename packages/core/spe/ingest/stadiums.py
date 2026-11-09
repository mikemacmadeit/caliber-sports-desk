"""NFL stadium coordinates for National Weather Service points.

Keyed by nflverse stadium_id, with name aliases for venues already stored
by stadium name. International sites are named explicitly so a domestic
stadium_id on a neutral-site game does not steal the forecast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Site:
    lat: float
    lon: float
    altitude_m: float = 0.0
    timezone: str = "America/New_York"


SITES: dict[str, Site] = {
    "ATL00": Site(33.7577, -84.4008, 300, "America/New_York"),
    "ATL97": Site(33.7554, -84.4010, 305, "America/New_York"),
    "BAL00": Site(39.2780, -76.6227, 10, "America/New_York"),
    "BOS00": Site(42.0909, -71.2643, 88, "America/New_York"),
    "BOS99": Site(42.0925, -71.2640, 88, "America/New_York"),
    "BRG00": Site(30.4120, -91.1838, 10, "America/Chicago"),
    "BUF00": Site(42.7738, -78.7870, 180, "America/New_York"),
    "BUF01": Site(43.6414, -79.3891, 76, "America/Toronto"),
    "CAR00": Site(35.2258, -80.8528, 230, "America/New_York"),
    "CHI98": Site(41.8623, -87.6167, 180, "America/Chicago"),
    "CHI99": Site(40.0992, -88.2359, 220, "America/Chicago"),
    "CIN00": Site(39.0955, -84.5161, 150, "America/New_York"),
    "CIN99": Site(39.0960, -84.5080, 150, "America/New_York"),
    "CLE00": Site(41.5061, -81.6995, 180, "America/New_York"),
    "DAL00": Site(32.7473, -97.0945, 180, "America/Chicago"),
    "DAL99": Site(32.8394, -96.9103, 150, "America/Chicago"),
    "DEN00": Site(39.7439, -105.0201, 1609, "America/Denver"),
    "DEN99": Site(39.7439, -105.0201, 1609, "America/Denver"),
    "DET00": Site(42.3400, -83.0456, 183, "America/Detroit"),
    "DET99": Site(42.6456, -83.2547, 290, "America/Detroit"),
    "FRA00": Site(50.0686, 8.6455, 100, "Europe/Berlin"),
    "GER00": Site(48.2188, 11.6247, 520, "Europe/Berlin"),
    "GNB00": Site(44.5013, -88.0622, 190, "America/Chicago"),
    "HOU00": Site(29.6847, -95.4107, 15, "America/Chicago"),
    "IND00": Site(39.7601, -86.1639, 220, "America/Indiana/Indianapolis"),
    "IND99": Site(39.7636, -86.1634, 220, "America/Indiana/Indianapolis"),
    "JAX00": Site(30.3239, -81.6373, 6, "America/New_York"),
    "KAN00": Site(39.0489, -94.4839, 264, "America/Chicago"),
    "LAX01": Site(33.9534, -118.3390, 40, "America/Los_Angeles"),
    "LAX97": Site(33.8644, -118.2611, 12, "America/Los_Angeles"),
    "LAX99": Site(34.0141, -118.2879, 50, "America/Los_Angeles"),
    "LON00": Site(51.5560, -0.2795, 50, "Europe/London"),
    "LON01": Site(51.4559, -0.3415, 15, "Europe/London"),
    "LON02": Site(51.6043, -0.0664, 15, "Europe/London"),
    "MAD01": Site(40.4531, -3.6883, 650, "Europe/Madrid"),
    "MEL00": Site(-37.8200, 144.9834, 20, "Australia/Melbourne"),
    "MEX00": Site(19.3029, -99.1505, 2230, "America/Mexico_City"),
    "MIA00": Site(25.9580, -80.2389, 3, "America/New_York"),
    "MIN00": Site(44.9739, -93.2581, 250, "America/Chicago"),
    "MIN01": Site(44.9738, -93.2577, 250, "America/Chicago"),
    "MIN98": Site(44.9765, -93.2247, 250, "America/Chicago"),
    "MUN01": Site(48.2188, 11.6247, 520, "Europe/Berlin"),
    "NAS00": Site(36.1665, -86.7713, 120, "America/Chicago"),
    "NOR00": Site(29.9511, -90.0812, 1, "America/Chicago"),
    "NYC00": Site(40.8128, -74.0742, 2, "America/New_York"),
    "NYC01": Site(40.8135, -74.0744, 2, "America/New_York"),
    "OAK00": Site(37.7516, -122.2005, 6, "America/Los_Angeles"),
    "PAR00": Site(48.9245, 2.3601, 40, "Europe/Paris"),
    "PHI00": Site(39.9008, -75.1675, 10, "America/New_York"),
    "PHI99": Site(39.9011, -75.1708, 10, "America/New_York"),
    "PHO00": Site(33.5276, -112.2626, 330, "America/Phoenix"),
    "PHO99": Site(33.4264, -111.9325, 360, "America/Phoenix"),
    "PIT00": Site(40.4468, -80.0158, 220, "America/New_York"),
    "PIT99": Site(40.4464, -80.0136, 220, "America/New_York"),
    "RIO00": Site(-22.9122, -43.2302, 5, "America/Sao_Paulo"),
    "SAN00": Site(29.4169, -98.4789, 200, "America/Chicago"),
    "SAO00": Site(-23.5453, -46.4742, 720, "America/Sao_Paulo"),
    "SDG00": Site(32.7831, -117.1196, 20, "America/Los_Angeles"),
    "SEA00": Site(47.5952, -122.3316, 5, "America/Los_Angeles"),
    "SEA98": Site(47.5952, -122.3316, 5, "America/Los_Angeles"),
    "SEA99": Site(47.6503, -122.3015, 10, "America/Los_Angeles"),
    "SFO00": Site(37.7136, -122.3861, 3, "America/Los_Angeles"),
    "SFO01": Site(37.4033, -121.9694, 5, "America/Los_Angeles"),
    "STL00": Site(38.6328, -90.1885, 140, "America/Chicago"),
    "TAM00": Site(27.9759, -82.5033, 5, "America/New_York"),
    "VEG00": Site(36.0909, -115.1833, 650, "America/Los_Angeles"),
    "WAS00": Site(38.9078, -76.8644, 50, "America/New_York"),
}

# Neutral-site names that must not inherit the home club's stadium_id.
_NAME_OVERRIDES: dict[str, str] = {
    "bernabeu": "MAD01",
    "santiago bernabeu": "MAD01",
    "fc bayern munich stadium": "MUN01",
    "allianz arena": "GER00",
    "wembley stadium": "LON00",
    "twickenham stadium": "LON01",
    "tottenham hotspur stadium": "LON02",
    "tottenham stadium": "LON02",
    "stade de france": "PAR00",
    "melbourne cricket ground": "MEL00",
    "estadio banorte": "MEX00",
    "azteca stadium": "MEX00",
    "maracana stadium": "RIO00",
    "deutsche bank park": "FRA00",
    "rogers centre": "BUF01",
}

_ALIASES: dict[str, str] = {
    "georgia dome": "ATL00",
    "mercedes benz stadium": "ATL97",
    "mt bank stadium": "BAL00",
    "m t bank stadium": "BAL00",
    "psinet stadium": "BAL00",
    "ravens stadium": "BAL00",
    "gillette stadium": "BOS00",
    "foxboro stadium": "BOS99",
    "tiger stadium lsu": "BRG00",
    "highmark stadium": "BUF00",
    "new era field": "BUF00",
    "ralph wilson stadium": "BUF00",
    "bank of america stadium": "CAR00",
    "ericsson stadium": "CAR00",
    "soldier field": "CHI98",
    "paycor stadium": "CIN00",
    "paul brown stadium": "CIN00",
    "huntington bank field": "CLE00",
    "firstenergy stadium": "CLE00",
    "cleveland browns stadium": "CLE00",
    "at t stadium": "DAL00",
    "att stadium": "DAL00",
    "cowboys stadium": "DAL00",
    "empower field at mile high": "DEN00",
    "invesco field at mile high": "DEN00",
    "sports authority field at mile high": "DEN00",
    "mile high stadium": "DEN99",
    "ford field": "DET00",
    "lambeau field": "GNB00",
    "nrg stadium": "HOU00",
    "reliant stadium": "HOU00",
    "lucas oil stadium": "IND00",
    "rca dome": "IND99",
    "everbank stadium": "JAX00",
    "everbank field": "JAX00",
    "tiaa bank stadium": "JAX00",
    "alltel stadium": "JAX00",
    "jacksonville municipal stadium": "JAX00",
    "geha field at arrowhead stadium": "KAN00",
    "arrowhead stadium": "KAN00",
    "sofi stadium": "LAX01",
    "hard rock stadium": "MIA00",
    "dolphin stadium": "MIA00",
    "sun life stadium": "MIA00",
    "pro player stadium": "MIA00",
    "us bank stadium": "MIN01",
    "u s bank stadium": "MIN01",
    "mall of america field": "MIN00",
    "hubert h humphrey metrodome": "MIN00",
    "nissan stadium": "NAS00",
    "lp field": "NAS00",
    "adelphiacoliseum": "NAS00",
    "the coliseum": "NAS00",
    "caesars superdome": "NOR00",
    "mercedes benz superdome": "NOR00",
    "louisiana superdome": "NOR00",
    "metlife stadium": "NYC01",
    "new meadowlands stadium": "NYC01",
    "giants stadium": "NYC00",
    "lincoln financial field": "PHI00",
    "state farm stadium": "PHO00",
    "university of phoenix stadium": "PHO00",
    "acrisure stadium": "PIT00",
    "heinz field": "PIT00",
    "lumen field": "SEA00",
    "centurylink field": "SEA00",
    "qwest field": "SEA00",
    "seahawks stadium": "SEA00",
    "levis stadium": "SFO01",
    "levi stadium": "SFO01",
    "allegiant stadium": "VEG00",
    "northwest stadium": "WAS00",
    "fedexfield": "WAS00",
    "fedex field": "WAS00",
    "raymond james stadium": "TAM00",
    "oakland alameda county coliseum": "OAK00",
    "ringcentral coliseum": "OAK00",
    "ring central coliseum": "OAK00",
    "qualcomm stadium": "SDG00",
    "edward jones dome": "STL00",
    "alamodome": "SAN00",
    "alamo dome": "SAN00",
    "los angeles memorial coliseum": "LAX99",
    "candlestick park": "SFO00",
}


def _norm(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def lookup_stadium(stadium_id: str | None, name: str | None) -> Site | None:
    named = _by_name(name)
    identified = SITES.get((stadium_id or "").strip().upper()) if stadium_id else None
    if named and identified and (named.lat != identified.lat or named.lon != identified.lon):
        return named
    return named or identified


def _by_name(name: str | None) -> Site | None:
    key = _norm(name)
    if not key:
        return None
    override = _NAME_OVERRIDES.get(key)
    if override:
        return SITES[override]
    alias = _ALIASES.get(key)
    if alias:
        return SITES[alias]
    for alias_key, site_id in _ALIASES.items():
        if len(alias_key) >= 12 and alias_key in key:
            return SITES[site_id]
    for alias_key, site_id in _NAME_OVERRIDES.items():
        if alias_key in key:
            return SITES[site_id]
    return None
