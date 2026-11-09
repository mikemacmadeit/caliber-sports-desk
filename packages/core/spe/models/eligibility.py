"""Pregame eligibility for historical evaluation.

Same-week roster files and snap counts are not treated as known before kickoff.
A missing injury report is not treated as healthy. A missing snap count is not a zero.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

SKILL = {"QB", "RB", "WR", "TE"}
EXPECTED_STATUS = {"ACT"}
INACTIVE_DESIGNATIONS = {"out", "ir", "injured reserve", "pup"}
UNCERTAIN_DESIGNATIONS = {"questionable", "doubtful"}


def _text(value: object) -> str:
    if value is None or value is pd.NA:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _designation(value: object) -> str:
    return _text(value).lower()


def availability_class(prior_status: object, designation: object) -> str:
    """Classify what a pregame bettor could have known. Confirmed active is never inferred."""
    label = _designation(designation)
    if label in INACTIVE_DESIGNATIONS:
        return "confirmed_inactive"
    if label in UNCERTAIN_DESIGNATIONS:
        return "uncertain"
    status = _text(prior_status).upper()
    if status in EXPECTED_STATUS:
        return "expected_active"
    return "unknown"


def include_in_corrected(has_stat_row: bool, availability: str) -> bool:
    """Recorded box-score rows stay, including zeros. Absences are added only when pregame status supports them."""
    if availability == "confirmed_inactive":
        return False
    if has_stat_row:
        return True
    return availability in {"expected_active", "uncertain"}


def summarize_eligibility(rows: pd.DataFrame) -> dict[str, Any]:
    """Count additions and exclusions. Participation is reported by the caller and is not an input."""
    if rows.empty:
        return {"rows": 0, "by_availability": {}, "added_recorded_zeros": 0, "added_absent": 0, "excluded_confirmed_inactive": 0}
    skill = rows["position_group"].isin(SKILL)
    involved = rows["involved"].fillna(False).astype(bool)
    has_stat = rows["has_stat_row"].fillna(False).astype(bool)
    availability = rows["availability"].fillna("unknown")
    included = pd.Series(
        [include_in_corrected(bool(stat), str(label)) for stat, label in zip(has_stat, availability, strict=False)],
        index=rows.index,
    )
    scope = rows[skill]
    included_scope = included[skill]
    return {
        "skill_stat_rows": int(has_stat[skill].sum()),
        "current_involved_skill_rows": int((skill & involved & has_stat).sum()),
        "added_recorded_zeros": int((skill & has_stat & ~involved & included_scope).sum()),
        "added_absent_expected_active": int((skill & ~has_stat & included_scope & availability.eq("expected_active")).sum()),
        "added_absent_uncertain": int((skill & ~has_stat & included_scope & availability.eq("uncertain")).sum()),
        "excluded_confirmed_inactive": int((skill & availability.eq("confirmed_inactive")).sum()),
        "excluded_inactive_with_stat_row": int((skill & has_stat & availability.eq("confirmed_inactive")).sum()),
        "excluded_unknown_absent": int((skill & ~has_stat & availability.eq("unknown")).sum()),
        "confirmed_active_pregame": 0,
        "by_availability": {label: int((skill & availability.eq(label)).sum()) for label in ("confirmed_inactive", "uncertain", "expected_active", "unknown")},
        "corrected_skill_rows": int(included_scope.sum()),
    }


def unique_prior_roster(roster: pd.DataFrame) -> pd.DataFrame:
    """Keep a prior week only when the player has one team and one status."""
    if roster.empty:
        return roster
    keys = ["player_id", "season_year", "week"]
    team_n = roster.groupby(keys)["team_id"].nunique()
    status_n = roster.groupby(keys)["status"].nunique(dropna=False)
    single = team_n[team_n.eq(1)].index.intersection(status_n[status_n.eq(1)].index)
    if len(single) == 0:
        return roster.iloc[0:0]
    kept = roster.drop_duplicates(keys)
    index = pd.MultiIndex.from_frame(kept[keys])
    return kept[index.isin(single)].reset_index(drop=True)


def label_stat_rows(stats: pd.DataFrame, roster: pd.DataFrame, injuries: pd.DataFrame) -> pd.DataFrame:
    """Attach availability from the previous week's roster and injuries reported before kickoff."""
    out = stats.copy()
    out["has_stat_row"] = True
    out["availability"] = "unknown"
    out["prior_status"] = pd.NA
    prior = unique_prior_roster(roster)
    if not prior.empty and "week" in out.columns:
        prior = prior.rename(columns={"week": "prior_week", "status": "prior_status", "team_id": "prior_team"})
        out["prior_week"] = pd.to_numeric(out["week"], errors="coerce") - 1
        out = out.merge(
            prior[["player_id", "season_year", "prior_week", "prior_status", "prior_team"]],
            on=["player_id", "season_year", "prior_week"],
            how="left",
            suffixes=("", "_roster"),
        )
        if "prior_status_roster" in out.columns:
            out["prior_status"] = out["prior_status"].fillna(out["prior_status_roster"])
            out = out.drop(columns=["prior_status_roster"])
    if not injuries.empty:
        latest = injuries.sort_values("reported_at").drop_duplicates(["game_id", "player_id"], keep="last")
        out = out.merge(latest[["game_id", "player_id", "designation"]], on=["game_id", "player_id"], how="left")
    else:
        out["designation"] = pd.NA
    out["availability"] = [
        availability_class(status, designation)
        for status, designation in zip(out["prior_status"], out.get("designation", pd.Series(pd.NA, index=out.index)), strict=False)
    ]
    counting = (
        out["targets"].fillna(0)
        + out["receptions"].fillna(0)
        + out["carries"].fillna(0)
        + out["rec_yards"].fillna(0).abs()
        + out["rush_yards"].fillna(0).abs()
    )
    out["involved"] = counting > 0
    return out


def absence_rows(games: pd.DataFrame, roster: pd.DataFrame, injuries: pd.DataFrame, stat_keys: pd.DataFrame) -> pd.DataFrame:
    """Players expected from last week's roster who have no box-score row. Outcomes are explicit zeros, not invented snaps."""
    prior = unique_prior_roster(roster)
    if prior.empty or games.empty:
        return pd.DataFrame()
    prior = prior.copy()
    prior["game_week"] = pd.to_numeric(prior["week"], errors="coerce") + 1
    prior = prior.drop(columns=["week"])
    slots = pd.concat(
        [games.assign(team_id=games["home_team_id"]), games.assign(team_id=games["away_team_id"])],
        ignore_index=True,
    )
    slots = slots.rename(columns={"week": "game_week"})
    merged = prior.merge(slots, on=["season_year", "game_week", "team_id"], how="inner")
    if stat_keys is not None and not stat_keys.empty:
        merged = merged.merge(stat_keys.assign(_stat=1), on=["game_id", "player_id"], how="left")
        merged = merged[merged["_stat"].isna()]
    if merged.empty:
        return pd.DataFrame()
    if not injuries.empty:
        latest = injuries.sort_values("reported_at").drop_duplicates(["game_id", "player_id"], keep="last")
        merged = merged.merge(latest[["game_id", "player_id", "designation"]], on=["game_id", "player_id"], how="left")
    else:
        merged["designation"] = pd.NA
    merged["availability"] = [
        availability_class(status, designation) for status, designation in zip(merged["status"], merged["designation"], strict=False)
    ]
    if merged.empty:
        return pd.DataFrame()
    merged["has_stat_row"] = False
    merged["involved"] = False
    for col, value in (
        ("targets", 0),
        ("receptions", 0),
        ("carries", 0),
        ("rec_yards", 0.0),
        ("rush_yards", 0.0),
        ("rush_td", 0),
        ("rec_td", 0),
        ("rz_targets", np.nan),
        ("rz_carries", np.nan),
        ("gl_carries", np.nan),
    ):
        merged[col] = value
    merged["anytime_tds"] = 0
    return merged
