"""Live check for the new keys. Does not print secrets."""

from spe.adapters.api_sports import ApiSportsAdapter
from spe.adapters.noaa_cdo import NoaaCdoAdapter
from spe.config import settings


def main() -> None:
    print("configured", bool(settings.api_sports_key), bool(settings.noaa_cdo_token))
    football = ApiSportsAdapter().get("/leagues", {"id": 39})
    league = football["response"][0]["league"]
    print("football", league["id"], league["name"], "rows", football.get("results"))
    american = ApiSportsAdapter().get("/leagues", sport="american-football")
    names = [row.get("name") or row.get("league", {}).get("name") for row in american["response"][:4]]
    print("american_football", american.get("results"), names)
    noaa = NoaaCdoAdapter().get("/datasets", {"limit": 1})
    first = (noaa.get("results") or [{}])[0].get("id")
    print("noaa", first)


if __name__ == "__main__":
    main()
