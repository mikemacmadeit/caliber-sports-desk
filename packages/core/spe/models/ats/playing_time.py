"""Who was lined up to play, using only a chart from before kickoff.

A missing chart stays missing. It is not a full healthy lineup.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

OFFENSE_POSITIONS = ("QB", "RB", "FB", "WR", "TE", "LT", "LG", "C", "RG", "RT")
OUT_DESIGNATIONS = ("Out", "Doubtful")
CHART_FEATURES = (
    "home_chart_known",
    "away_chart_known",
    "home_chart_out",
    "away_chart_out",
)


def load_nfl_chart_out(session: Session) -> pd.DataFrame:
    """Starters on the previous chart who are Out or Doubtful before this kickoff."""
    charts = pd.read_sql(
        text(
            """
            SELECT season_year, week, team_id, player_id, position
            FROM depth_chart_weeks
            WHERE depth_rank = 1
              AND position IN ('QB', 'RB', 'FB', 'WR', 'TE', 'LT', 'LG', 'C', 'RG', 'RT')
            """
        ),
        session.connection(),
    )
    games = pd.read_sql(
        text(
            """
            SELECT game_id, kickoff_utc, week, season_id, home_team_id, away_team_id
            FROM games
            WHERE league_id = 'nfl'
              AND data_tier IN ('historical', 'upcoming')
            """
        ),
        session.connection(),
    )
    injuries = pd.read_sql(
        text(
            """
            SELECT i.game_id, i.player_id
            FROM injury_events i
            JOIN games g ON g.game_id = i.game_id
            WHERE g.league_id = 'nfl'
              AND i.reported_at <= g.kickoff_utc
              AND i.designation IN ('Out', 'Doubtful')
            """
        ),
        session.connection(),
    )
    if games.empty:
        return _empty()
    games["kickoff_utc"] = pd.to_datetime(games["kickoff_utc"], utc=True)
    games["season_year"] = games["season_id"].astype(str).str.rsplit("-", n=1).str[-1].astype(int)
    week_charts, date_charts = _index_charts(charts)
    hurt = {game_id: set(rows["player_id"]) for game_id, rows in injuries.groupby("game_id")} if not injuries.empty else {}
    rows = []
    for game in games.itertuples(index=False):
        home_known, home_out = _side(game, game.home_team_id, week_charts, date_charts, hurt)
        away_known, away_out = _side(game, game.away_team_id, week_charts, date_charts, hurt)
        rows.append(
            {
                "game_id": game.game_id,
                "home_chart_known": home_known,
                "away_chart_known": away_known,
                "home_chart_out": home_out,
                "away_chart_out": away_out,
            }
        )
    return pd.DataFrame(rows)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["game_id", *CHART_FEATURES])


def _index_charts(charts: pd.DataFrame) -> tuple[dict, dict]:
    week_charts: dict[tuple[int, int, str], set[str]] = {}
    dated: dict[str, list[tuple[int, set[str]]]] = {}
    if charts.empty:
        return week_charts, dated
    charts = charts.drop_duplicates(["season_year", "week", "team_id", "player_id"])
    for (season, week, team), group in charts.groupby(["season_year", "week", "team_id"]):
        players = set(group["player_id"])
        if int(week) <= 30:
            week_charts[(int(season), int(week), str(team))] = players
        else:
            dated.setdefault(str(team), []).append((int(week), players))
    for team, items in dated.items():
        items.sort()
    return week_charts, dated


def _side(game, team_id: str, week_charts: dict, date_charts: dict, hurt: dict) -> tuple[float, float]:
    players = _prior_starters(game, str(team_id), week_charts, date_charts)
    if players is None:
        return 0.0, 0.0
    out = players & hurt.get(game.game_id, set())
    return 1.0, float(min(6, len(out)))


def _prior_starters(game, team_id: str, week_charts: dict, date_charts: dict) -> set[str] | None:
    kick = pd.Timestamp(game.kickoff_utc)
    kick_key = int(kick.strftime("%Y%m%d"))
    latest: set[str] | None = None
    for stamp, players in date_charts.get(team_id, []):
        if stamp < kick_key:
            latest = players
        else:
            break
    if latest is not None:
        return latest
    season = int(game.season_year)
    week = int(game.week) if game.week is not None and not pd.isna(game.week) else None
    if week is None or week > 30:
        return None
    for prev in range(week - 1, 0, -1):
        found = week_charts.get((season, prev, team_id))
        if found:
            return found
    for prev in range(30, 0, -1):
        found = week_charts.get((season - 1, prev, team_id))
        if found:
            return found
    return None
