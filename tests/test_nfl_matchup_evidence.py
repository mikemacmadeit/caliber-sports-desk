from datetime import timedelta
import json
import os
from types import SimpleNamespace

import pandas as pd
import pytest

from spe.ingest.nfl_matchup_evidence import aggregate, refresh
from spe.db.base import as_utc
from test_nflverse_desk import play
from test_free_sources import _game


def test_new_matchup_source_counts_real_plays_and_does_not_use_defensive_tds():
    kickoff=as_utc('2024-09-01T18:00:00Z')
    game=SimpleNamespace(home_team_id='nfl:NYJ',away_team_id='nfl:BUF',kickoff_utc=kickoff)
    first=play(1,pass_attempt=1,rush_attempt=0,yards_gained=5,game_seconds_remaining=3500,posteam_score=0,defteam_score=0)
    returned=play(2,pass_attempt=1,rush_attempt=0,touchdown=1,interception=1,
        receiver_player_id=None,yards_gained=0,game_seconds_remaining=3475,posteam_score=0,defteam_score=0)
    future=play(3,game_id='2024_02_BUF_NYJ',epa=999)
    teams,players=aggregate(pd.DataFrame([first,first,returned,future]),{'nfl:2024_01_BUF_NYJ':game},kickoff+timedelta(days=3))
    assert len(teams)==1 and teams[0]['plays']==2
    assert teams[0]['red_zone_td_rate']==0
    assert teams[0]['pass_epa']==pytest.approx(.2)
    assert players[0]['targets']==1
    assert players[0]['player_id']=='nfl:00-0034796'


def test_refresh_rejects_future_observations_and_retains_other_seasons(db):
    from spe.config import settings
    _game(db)
    now=as_utc('2024-10-01T18:00:00Z')
    source=settings.parquet_dir.resolve().parent/'cache/nflverse/play_by_play_2024.parquet'
    source.parent.mkdir(parents=True,exist_ok=True)
    pd.DataFrame([play(pass_attempt=1,rush_attempt=0)]).to_parquet(source)
    target=settings.parquet_dir/'desk_evidence/nfl.json';target.parent.mkdir(parents=True,exist_ok=True)
    prior={'teams':[{'game_id':'old','team_id':'nfl:BUF','opponent_id':'nfl:NYJ','kickoff_utc':'2023-09-01T18:00:00Z'}],'players':[]}
    target.write_text(json.dumps(prior))
    os.utime(source,(now.timestamp()+1,now.timestamp()+1))
    assert refresh(db,year=2024,now=now)['status']=='future_source_rejected'
    assert json.loads(target.read_text())==prior
    os.utime(source,(now.timestamp()-60,now.timestamp()-60))
    report=refresh(db,year=2024,now=now)
    assert report['status']=='updated'
    assert any(r['game_id']=='old' for r in json.loads(target.read_text())['teams'])
    assert refresh(db,year=2024,now=now)['status']=='fresh'
