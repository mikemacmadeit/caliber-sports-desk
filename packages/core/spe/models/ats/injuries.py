"""Pregame injury counts. A missing report stays missing; it is not a clean bill of health."""

from __future__ import annotations

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

SKILL_POSITIONS = ("WR", "RB", "TE")


def load_injury_features(session: Session, league_id: str) -> pd.DataFrame:
    """One row per game that has a report filed before kickoff.

    Counts only Out and Doubtful players whose roster says they were on the
    home or away team at kickoff. A missing report stays missing.
    """
    raw = pd.read_sql(
        text(
            """
            SELECT DISTINCT
                   i.game_id,
                   g.home_team_id,
                   g.away_team_id,
                   r.team_id,
                   p.position
            FROM injury_events i
            JOIN games g ON g.game_id = i.game_id
            JOIN players p ON p.player_id = i.player_id
            JOIN rosters r
              ON r.player_id = i.player_id
             AND r.team_id IN (g.home_team_id, g.away_team_id)
             AND r.valid_from <= g.kickoff_utc
             AND (r.valid_to IS NULL OR r.valid_to >= g.kickoff_utc)
            WHERE g.league_id = :lg
              AND i.reported_at <= g.kickoff_utc
              AND lower(i.designation) IN ('out', 'doubtful')
            """
        ),
        session.connection(),
        params={"lg": league_id},
    )
    if raw.empty:
        return pd.DataFrame(
            columns=[
                "game_id",
                "injury_report",
                "home_qb_out",
                "away_qb_out",
                "home_skill_out",
                "away_skill_out",
            ]
        )
    raw["is_home"] = raw["team_id"] == raw["home_team_id"]
    raw["is_qb"] = raw["position"] == "QB"
    raw["is_skill"] = raw["position"].isin(SKILL_POSITIONS)
    grouped = (
        raw.groupby("game_id")
        .apply(_counts, include_groups=False)
        .reset_index()
    )
    grouped["injury_report"] = 1.0
    return grouped


def load_nfl_injury_features(session: Session) -> pd.DataFrame:
    """NFL reports. College uses the same counts through ``load_injury_features``."""
    return load_injury_features(session, "nfl")


def _counts(group: pd.DataFrame) -> pd.Series:
    home = group["is_home"]
    return pd.Series(
        {
            "home_qb_out": float((home & group["is_qb"]).sum()),
            "away_qb_out": float((~home & group["is_qb"]).sum()),
            "home_skill_out": float(min(4, int((home & group["is_skill"]).sum()))),
            "away_skill_out": float(min(4, int((~home & group["is_skill"]).sum()))),
        }
    )


INJURY_FEATURES = (
    "injury_report",
    "home_qb_out",
    "away_qb_out",
    "home_skill_out",
    "away_skill_out",
)
