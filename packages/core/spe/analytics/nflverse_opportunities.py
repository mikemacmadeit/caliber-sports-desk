"""Free NFL game inputs. No FTN fields, route counts, or inferred player identities.

End-zone targets are explicitly estimates from air yards and field position.
Targets per dropback are not targets per route. Red-zone trips count distinct drives.
The functions are shared by ingestion and retrospective model evaluation.
"""
from __future__ import annotations

import math
from collections import defaultdict

import pandas as pd

from spe.ingest.canonical import nfl_game_id, nfl_player_id, nfl_team_id
from spe.ingest.values import as_str


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def ratio(top, bottom):
    return float(top / bottom) if bottom else None


def _series(frame, key):
    return pd.to_numeric(frame.get(key, pd.Series(index=frame.index, dtype=float)), errors="coerce")


def pbp_context(frame: pd.DataFrame, positions: dict[str, str]) -> list[dict]:
    required = {"game_id", "play_id", "posteam", "defteam", "play_type", "yardline_100",
                "receiver_player_id", "rusher_player_id"}
    if frame.empty:
        return []
    if not required.issubset(frame.columns):
        raise ValueError(f"Missing PBP columns: {sorted(required - set(frame.columns))}")
    df = frame.drop_duplicates(["game_id", "play_id"], keep="last").copy()
    valid = df["play_type"].isin(["run", "pass"])
    for flag in ("no_play", "two_point_attempt", "extra_point_attempt", "qb_kneel", "qb_spike"):
        valid &= ~_series(df, flag).eq(1)
    df = df.loc[valid & df["game_id"].notna() & df["posteam"].notna() & df["defteam"].notna()].copy()
    df["rush"] = df["play_type"].eq("run")
    df["pass"] = df["play_type"].eq("pass")
    df["target"] = df["pass"] & df["receiver_player_id"].notna() & ~_series(df, "sack").eq(1)
    df["carry"] = df["rush"] & df["rusher_player_id"].notna()
    df["yl"] = _series(df, "yardline_100").where(lambda x: x.between(0, 100))
    df["air"] = _series(df, "air_yards")
    df["air_known"] = df["target"] & df["air"].notna() & df["yl"].notna()
    df["ez_est"] = df["air_known"] & df["air"].ge(df["yl"])
    df["epa_num"] = _series(df, "epa")
    df["neutral"] = _series(df, "score_differential").abs().le(7) & _series(df, "qtr").le(3)
    df["drive_key"] = df["fixed_drive"] if "fixed_drive" in df else df.get("drive", pd.Series(index=df.index, dtype=float))
    from spe.analytics.td_scoring_paths import aggregate
    scoring_paths = aggregate(df, positions)
    records = []
    for (external, abbr), plays in df.groupby(["game_id", "posteam"], sort=False):
        game, team = nfl_game_id(str(external)), nfl_team_id(str(abbr))
        team_metrics = {"plays": len(plays), "dropbacks": int(plays["pass"].sum()),
                        "carries": int(plays["carry"].sum()), "targets": int(plays["target"].sum()),
                        "field_position_coverage": float(plays["yl"].notna().mean()),
                        "air_yards_targets": int(plays["air_known"].sum())}
        for distance in (5, 10, 20):
            near = plays["yl"].le(distance)
            for kind, plural in (("carry", "carries"), ("target", "targets")):
                team_metrics[f"inside{distance}_{plural}"] = int((near & plays[kind]).sum())
        team_metrics["end_zone_targets_estimated"] = int(plays["ez_est"].sum()) if plays["air_known"].any() else None
        drives_known = plays["drive_key"].notna().all()
        team_metrics["red_zone_drives"] = int(plays.loc[plays["yl"].le(20), "drive_key"].nunique()) if drives_known else None
        team_metrics["drives"] = int(plays["drive_key"].nunique()) if drives_known else None
        neutral = plays.loc[plays["neutral"]]
        team_metrics["neutral_pass_rate"] = float(neutral["pass"].mean()) if len(neutral) else None
        for kind in ("pass", "rush"):
            team_metrics[f"{kind}_epa"] = number(plays.loc[plays[kind], "epa_num"].mean())
        participants = set(plays.loc[plays["target"], "receiver_player_id"]) | set(plays.loc[plays["carry"], "rusher_player_id"])
        positional = defaultdict(int)
        for pid in sorted(participants):
            target = plays["target"] & plays["receiver_player_id"].eq(pid)
            carry = plays["carry"] & plays["rusher_player_id"].eq(pid)
            m = {"targets": int(target.sum()), "carries": int(carry.sum())}
            for distance in (5, 10, 20):
                near = plays["yl"].le(distance)
                m[f"inside{distance}_carries"] = int((near & carry).sum())
                m[f"inside{distance}_targets"] = int((near & target).sum())
            air_known = target & plays["air_known"]
            m["air_yards_targets"] = int(air_known.sum())
            m["end_zone_targets_estimated"] = int((target & plays["ez_est"]).sum()) if air_known.any() else None
            m["air_yards_coverage"] = ratio(air_known.sum(), target.sum())
            m["average_target_depth"] = number(plays.loc[target, "air"].mean())
            m["target_share"] = ratio(m["targets"], team_metrics["targets"])
            m["targets_per_dropback"] = ratio(m["targets"], team_metrics["dropbacks"])
            m["carry_share"] = ratio(m["carries"], team_metrics["carries"])
            m["inside5_carry_share"] = ratio(m["inside5_carries"], team_metrics["inside5_carries"])
            m["inside10_target_share"] = ratio(m["inside10_targets"], team_metrics["inside10_targets"])
            pos = positions.get(str(pid), "UNK")
            pos = "RB" if pos == "FB" else pos
            for metric in ("targets", "inside10_targets", "inside5_carries"):
                positional[f"{pos}_{metric}"] += m[metric]
            m['scoring_paths'] = scoring_paths.get((str(external), str(abbr), str(pid)))
            records.append({"game_id": game, "entity_id": nfl_player_id(str(pid)), "team_id": team,
                            "dataset": "pbp_player", "metrics": m})
        for pos in ("QB", "RB", "WR", "TE", "UNK"):
            for metric in ("targets", "inside10_targets", "inside5_carries"):
                team_metrics[f"{pos}_{metric}"] = positional[f"{pos}_{metric}"]
        team_metrics['scoring_paths'] = scoring_paths.get((str(external), str(abbr), '__team__'))
        records.append({"game_id": game, "entity_id": team, "team_id": team,
                        "dataset": "pbp_team", "metrics": team_metrics})
    return records


def snap_context(frame: pd.DataFrame, players: pd.DataFrame) -> tuple[list[dict], int]:
    if frame.empty:
        return [], 0
    required = {"game_id", "pfr_player_id", "team", "offense_snaps", "offense_pct"}
    if not required.issubset(frame.columns) or not {"gsis_id", "pfr_id"}.issubset(players.columns):
        raise ValueError("Snap counts need exact PFR-to-GSIS IDs and offensive snaps/share.")
    ids = players.dropna(subset=["gsis_id", "pfr_id"])[["pfr_id", "gsis_id"]].drop_duplicates()
    ids = ids.loc[~ids["pfr_id"].duplicated(keep=False)]
    mapping = dict(zip(ids["pfr_id"], ids["gsis_id"]))
    records, skipped = [], 0
    for row in frame.drop_duplicates(["game_id", "pfr_player_id", "team"], keep="last").to_dict("records"):
        pid = mapping.get(row["pfr_player_id"])
        share, snaps = number(row["offense_pct"]), number(row["offense_snaps"])
        if not pid or not as_str(row["game_id"]) or not as_str(row["team"]) or share is None or snaps is None or not 0 <= share <= 1 or snaps < 0:
            skipped += 1
            continue
        records.append({"game_id": nfl_game_id(str(row["game_id"])), "entity_id": nfl_player_id(str(pid)),
                        "team_id": nfl_team_id(str(row["team"])), "dataset": "snap_counts",
                        "metrics": {"offense_snaps": snaps, "offense_snap_share": share}})
    return records, skipped
