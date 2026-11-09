from __future__ import annotations

from spe.adapters.api_sports import ApiSportsAdapter
from spe.adapters.base import ProviderAdapter
from spe.adapters.cfbd import CfbdAdapter
from spe.adapters.gdelt import GdeltAdapter
from spe.adapters.nflverse import NflverseAdapter
from spe.adapters.noaa_cdo import NoaaCdoAdapter
from spe.adapters.nws import NwsAdapter
from spe.adapters.odds_api import OddsApiAdapter
from spe.adapters.open_meteo import OpenMeteoAdapter

_ADAPTERS: dict[str, type[ProviderAdapter]] = {
    "api_sports": ApiSportsAdapter,
    "cfbd": CfbdAdapter,
    "gdelt": GdeltAdapter,
    "nflverse": NflverseAdapter,
    "noaa_cdo": NoaaCdoAdapter,
    "open_meteo": OpenMeteoAdapter,
    "odds_api": OddsApiAdapter,
    "nws": NwsAdapter,
}


def get_adapter(provider_id: str) -> ProviderAdapter:
    if provider_id not in _ADAPTERS:
        raise KeyError(f"No adapter registered for '{provider_id}'")
    return _ADAPTERS[provider_id]()


def list_adapters() -> list[str]:
    return sorted(_ADAPTERS)
