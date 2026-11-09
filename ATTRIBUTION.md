# Attribution and license notes

This prototype records source license metadata on every adapter. Private research and commercial redistribution are different regimes.

## nflverse

- Data releases: https://github.com/nflverse/nflverse-data
- License: CC-BY-4.0 on nflverse-data
- Attribute CC-BY datasets when used.
- Do not include FTN / CC-BY-SA-derived features in a proprietary app pending license review.

## CollegeFootballData

- API: https://api.collegefootballdata.com/
- Tiers: https://collegefootballdata.com/api-tiers
- Commercial redisplay requires contractual confirmation before public launch.

## The Odds API

- https://the-odds-api.com/
- Markets: https://the-odds-api.com/sports-odds-data/betting-markets.html
- Credits are not HTTP requests. Do not redisplay bookmaker feeds without rights.

## Weather

- NWS/NOAA: https://www.weather.gov/documentation/services-web-api
- Open-Meteo previous-day forecasts are CC BY 4.0 for noncommercial use. Commercial use needs a paid license: https://open-meteo.com/en/pricing
- NOAA Climate Data Online needs a free token: https://www.ncei.noaa.gov/cdo-web/webservices/v2
- Daily NOAA observations are not kickoff forecasts.

## News

- GDELT DOC 2.0: https://www.gdeltproject.org/
- Stored articles keep `seendate` and are not verified injury reports.

## nflverse additions

- Participation, Next Gen Stats summaries, weekly rosters, and depth charts ship in nflverse-data under CC-BY-4.0.
- Next Gen Stats files here are season/week summaries, not tracking coordinates.
- Play-level route labels are not player route charts.

## Deferred commercial vendors

PFF, Sports Info Solutions, SportsDataIO, Sportradar/Genius — contract first, then integrate behind the same provider adapter interface.
