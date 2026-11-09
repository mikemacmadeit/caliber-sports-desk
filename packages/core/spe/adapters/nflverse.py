from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from spe.adapters.base import AdapterError, FetchResult, ProviderAdapter

# nflverse-data GitHub releases. Prefer parquet. Do not pull FTN/CC-BY-SA assets.
RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"

RELEASES = {
    "schedules": ("schedules", "games.parquet"),
    "pbp": ("pbp", "play_by_play_{season}.parquet"),
    "player_stats": ("stats_player", "stats_player_week_{season}.parquet"),
    "players": ("players", "players.parquet"),
    "rosters": ("rosters", "roster_{season}.parquet"),
    "injuries": ("injuries", "injuries_{season}.parquet"),
    "participation": ("pbp_participation", "pbp_participation_{season}.parquet"),
    "ngs_passing": ("nextgen_stats", "ngs_passing.parquet"),
    "ngs_rushing": ("nextgen_stats", "ngs_rushing.parquet"),
    "ngs_receiving": ("nextgen_stats", "ngs_receiving.parquet"),
    "roster_weekly": ("weekly_rosters", "roster_weekly_{season}.parquet"),
    "depth_charts": ("depth_charts", "depth_charts_{season}.parquet"),
}


class NflverseAdapter(ProviderAdapter):
    provider_id = "nflverse"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": True,
            "license": self.license.license_name,
            "avoid": list(self.license.avoid_features),
            "attribution_required": True,
        }

    def asset_url(self, key: str, season: int | None = None) -> str:
        if key not in RELEASES:
            raise AdapterError(f"Unknown nflverse release '{key}'")
        tag, filename = RELEASES[key]
        if "{season}" in filename:
            if season is None:
                raise AdapterError(f"Season required for {key}")
            filename = filename.format(season=season)
        return f"{RELEASE_BASE}/{tag}/{filename}"

    def fetch(
        self,
        key: str,
        *,
        season: int | None = None,
        fixture: list[dict[str, Any]] | None = None,
    ) -> FetchResult:
        observed = datetime.now(UTC)
        if fixture is not None:
            records = [self._normalize(key, r) for r in fixture]
            return FetchResult(records=records, observed_at=observed, endpoint=key, raw=fixture)
        url = self.asset_url(key, season)
        client = self._client or httpx.Client(timeout=60.0, follow_redirects=True)
        owned = self._client is None
        try:
            res = client.get(url)
            res.raise_for_status()
        except httpx.HTTPError as exc:
            raise AdapterError(f"nflverse download failed for {url}: {exc}") from exc
        finally:
            if owned:
                client.close()
        # Live parquet parse happens in the worker; adapters return URL + bytes length here.
        return FetchResult(
            records=[{"url": url, "bytes": len(res.content), "content_type": res.headers.get("content-type")}],
            observed_at=observed,
            endpoint=url,
            raw={"url": url, "status": res.status_code},
        )

    def _normalize(self, key: str, raw: dict[str, Any]) -> dict[str, Any]:
        if key == "schedules":
            return {
                "provider": self.provider_id,
                "provider_record_id": str(raw.get("game_id") or ""),
                "season": raw.get("season"),
                "week": raw.get("week"),
                "home_team": raw.get("home_team"),
                "away_team": raw.get("away_team"),
                "kickoff": raw.get("gameday") or raw.get("start_time"),
                "stadium": raw.get("stadium"),
                "roof": raw.get("roof"),
                "surface": raw.get("surface"),
                "classification": "nfl",
                "raw": raw,
            }
        return {"provider": self.provider_id, "raw": raw}
