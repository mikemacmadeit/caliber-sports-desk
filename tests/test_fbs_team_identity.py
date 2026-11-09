"""FBS scoreboard ids and historical ids for the same club resolve together."""

from types import SimpleNamespace

from spe.ingest.cfbd_historical import _remember_team_name
from spe.ingest.espn_scoreboard import pick_scoreboard_team
from spe.integrity.assignments import canonical_team_map, same_club
from spe.models.ats.guards import withhold_for_history_gap


def _team(team_id: str, league_id: str = "fbs", aliases: list | None = None, abbreviation: str = "UGA"):
    return SimpleNamespace(
        team_id=team_id,
        league_id=league_id,
        name=team_id,
        abbreviation=abbreviation,
        aliases=aliases or [],
    )


def test_short_id_and_historical_id_are_one_club():
    uga = _team("uga", aliases=[{"espn_id": "61"}])
    historical = _team("fbs:61")
    ole_miss = _team("fbs:145", abbreviation="MISS")
    canon = canonical_team_map([uga, historical, ole_miss])
    assert canon["uga"] == "fbs:61"
    assert canon["fbs:61"] == "fbs:61"
    assert canon["fbs:145"] == "fbs:145"
    assert same_club("uga", "fbs:61", canon)


def test_nfl_ids_are_left_alone():
    rams = _team("nfl:LA", league_id="nfl", abbreviation="LA")
    assert canonical_team_map([rams]) == {}


def test_scoreboard_prefers_historical_id():
    chosen = pick_scoreboard_team(
        [_team("uga", aliases=[{"espn_id": "61"}]), _team("fbs:61")],
        "fbs",
        "61",
    )
    assert chosen.team_id == "fbs:61"


def test_live_short_ids_point_at_a_real_history():
    import json
    import sqlite3
    from pathlib import Path

    db = Path("data/caliber.db")
    if not db.exists():
        return
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT team_id, name, abbreviation, aliases FROM teams WHERE league_id = 'fbs'"
    ).fetchall()
    con.close()
    teams = []
    for team_id, name, abbreviation, raw in rows:
        try:
            aliases = json.loads(raw or "[]")
        except json.JSONDecodeError:
            aliases = []
        teams.append(_team(team_id, aliases=aliases, abbreviation=abbreviation))
        teams[-1].name = name
    canon = canonical_team_map(teams)
    shorts = [team for team in teams if ":" not in team.team_id]
    assert shorts
    for team in shorts:
        target = canon[team.team_id]
        assert target != team.team_id
        assert target.startswith("fbs:") and target.split(":", 1)[1].isdigit()


def test_a_split_history_cannot_be_published_as_a_score():
    # Ole Miss had 122 prior games and Georgia's short id had 8.
    assert withhold_for_history_gap(122, 8)
    assert not withhold_for_history_gap(122, 123)


def test_name_map_keeps_historical_id():
    names: dict[str, str] = {}
    _remember_team_name(names, "uga", "uga")
    _remember_team_name(names, "uga", "fbs:61")
    _remember_team_name(names, "uga", "uga")
    assert names["uga"] == "fbs:61"
