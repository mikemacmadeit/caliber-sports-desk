"""Free historical market inputs; never masquerade as observed pregame quotes."""
import hashlib
import json
import math
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

BASE = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_cfb_betting'
TRUSTED_SOURCES = {'summary_pickcenter', 'core_odds_api'}


def normalize(records, observed_at, source_url):
    result = {}
    for row in records:
        if row.get('odds_source') not in TRUSTED_SOURCES or row.get('game_spread_available') is not True:
            continue
        try:
            event = int(row['game_id'])
            total, spread = float(row['over_under']), float(row['home_team_spread'])
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        if not all(map(math.isfinite, (total, spread))) or not (0 < total < 150 and abs(spread) < total):
            continue
        # Read the explicit home handicap, not the favorite's unsigned spread.
        result[str(event)] = {'home_handicap': spread, 'total': total,
            'source': 'sportsdataverse_market_archive', 'upstream_source': row['odds_source'],
            'observed_at': observed_at, 'source_url': source_url,
            'retrospective_market_data': True, 'pregame_timestamp_verified': False}
    return result


def refresh(years, directory):
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    markets, sources = {}, []
    for year in years:
        url = f'{BASE}/betting_{year}.parquet'
        path = directory / f'betting_{year}.parquet'
        meta = path.with_suffix('.meta.json')
        if not path.exists():
            request = urllib.request.Request(url, headers={'User-Agent': 'caliber-research/1.0'})
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
            temp = path.with_suffix('.tmp'); temp.write_bytes(body); temp.replace(path)
            meta.write_text(json.dumps({'observed_at':datetime.now(UTC).isoformat(), 'source_url':url,
                                       'sha256':hashlib.sha256(body).hexdigest()}))
        metadata = json.loads(meta.read_text()) if meta.exists() else {
            'observed_at':datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            'source_url':url, 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        frame = pd.read_parquet(path)
        parsed = normalize(frame.to_dict('records'), metadata['observed_at'], url)
        markets.update(parsed)
        sources.append({**metadata, 'year':year, 'rows':len(frame), 'usable_games':len(parsed)})
    result = {'markets':markets, 'sources':sources,
              'limitation':'Retrospective resolved lines, not executable prices or verified pregame snapshots.'}
    (directory/'markets.json').write_text(json.dumps(result))
    return result
