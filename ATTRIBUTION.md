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

- Check the upstream license of each dataset; the repository license is not a blanket license for every upstream feed.
- PBP-derived Desk metrics credit nflverse; snap counts credit Pro Football Reference via nflverse; NGS summaries credit NFL Next Gen Stats via nflverse.
- Participation from 2023 onward comes from FTN and is released after the postseason. Do not label it a current weekly feed or assume the blanket CC-BY license above applies.
- The separate free FTN charting subset is CC-BY-SA-4.0 with attribution to FTN Data via nflverse. It remains excluded from the proprietary Desk under this project's existing license policy. This is a project policy, not a claim that CC-BY-SA prohibits commercial use.
- Next Gen Stats files here are season/week summaries, not tracking coordinates.
- Play-level route labels describe the primary receiver; they do not measure every eligible player's routes run.
- Derived end-zone targets use air yards and field position. Label them as estimates, with missing air yards retained as unknown.
- Official availability and field definitions: https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html and https://nflreadr.nflverse.com/articles/dictionary_participation.html

## Deferred commercial vendors

- Additional local college context: score-state passing rates, short-yardage success, sacks and primary-passer continuity derived from the same SportsDataverse ESPN play-by-play. These are aggregate proxies, not individual blocking grades or confirmed game plans.
- Current college roster identities: public ESPN team roster endpoint, cached for 24 hours with first-observed timestamps. Membership does not imply starter status, participation or health. No NCAA depth-chart ranks are fabricated from roster order.

- College player opportunities: ESPN-derived play-by-play distributed by SportsDataverse, https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_cfb_pbp. The repository's MIT license does not establish blanket rights over upstream ESPN content. This local integration stores source URLs, retrieval timestamps and hashes; public deployment/redistribution has not been reviewed.
- SEC, ACC and American availability: public conference reports served by HD Intelligence's public report endpoint, linked from https://www.secsports.com/fbreports and https://theamerican.org/sports/2026/8/6/football_player_availability.aspx. Only normalized player statuses are retained, with publication and first-observed timestamps; no embedded images are stored. Missing reports are unknown.
- College historical spreads/totals: ESPN-derived SportsDataverse `espn_cfb_betting` release, https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_cfb_betting. Default and manually injected rows are rejected. Source URLs, retrieval timestamps and hashes are retained. These are retrospective resolved lines without verified pregame observation times, not executable betting prices. The same upstream-content and local-use limitations above apply.

PFF, Sports Info Solutions, SportsDataIO, Sportradar/Genius — contract first, then integrate behind the same provider adapter interface.
