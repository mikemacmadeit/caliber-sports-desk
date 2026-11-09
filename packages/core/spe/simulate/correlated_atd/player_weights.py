"""Player opportunity weights for correlated TD allocation.

Uses M6 player rates when present, plus verified prior usage from completed games.
Never fabricates FBS RZ/GL. Never includes confirmed-ineligible / retired players.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.models import Game, PlayerGameStat
from spe.identity.roster import EligiblePlayer, eligible_players_for_game
from spe.models.metrics import apply_platt
from spe.models.player_td.integration import TeamScoringContext
from spe.models.player_td.shares import DEFAULT_OTHER_SHARE, default_weights

SKILL = {"QB", "RB", "WR", "TE"}


@dataclass
class PlayerOpportunity:
    player_id: str
    team_id: str
    name: str
    position_group: str
    eligibility: str
    availability: float
    rush_weight: float
    rec_weight: float
    m6_p: float | None
    m6_mean: float | None
    prior_td_rate: float
    prior_carries: float
    prior_targets: float
    prior_gl_carries: float
    prior_rz_targets: float
    snap_share: float
    notes: list[str] = field(default_factory=list)


def load_m6(league_id: str) -> dict[str, Any] | None:
    path = Path(settings.parquet_dir) / "models" / "player_td" / f"{league_id}_m6_player_td.json"
    if not path.exists():
        alt = Path(settings.parquet_dir) / "models" / f"{league_id}_m6_player_td.json"
        path = alt if alt.exists() else path
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _m6_predict_simple(art: dict[str, Any], player_id: str, position: str) -> tuple[float | None, float | None]:
    rates = art.get("player_rates") or {}
    ns = art.get("player_n") or {}
    pos_rates = art.get("pos_rates") or {}
    league = float(art.get("league_rate") or 0.15)
    k = float(art.get("k_player") or art.get("extras", {}).get("k_player") or 12.0)
    raw = rates.get(player_id)
    n = int(ns.get(player_id, 0))
    prior = float(pos_rates.get(position, league))
    if raw is None and n == 0:
        mean = prior
    else:
        shrink = n / (n + k) if n > 0 else 0.0
        mean = shrink * float(raw if raw is not None else prior) + (1.0 - shrink) * prior
    mean = max(1e-6, float(mean))
    p_raw = float(1.0 - np.exp(-mean))
    a, b = 0.0, 1.0
    extras = art.get("extras") if isinstance(art.get("extras"), dict) else {}
    if extras and extras.get("platt") and len(extras["platt"]) == 2:
        a, b = float(extras["platt"][0]), float(extras["platt"][1])
    else:
        a = float(art.get("platt_a") or 0.0)
        b = float(art.get("platt_b") or 1.0)
    platt_pos = (art.get("platt_by_pos") or {}).get(position)
    if platt_pos and len(platt_pos) == 2:
        a, b = float(platt_pos[0]), float(platt_pos[1])
    p_cal = float(apply_platt(np.array([p_raw]), a, b)[0])
    return p_cal, mean


def player_history_priors(session: Session, league_id: str, player_ids: set[str]) -> pd.DataFrame:
    if not player_ids:
        return pd.DataFrame()
    q = (
        session.query(
            PlayerGameStat.player_id.label("player_id"),
            func.avg(PlayerGameStat.carries).label("prior_carries"),
            func.avg(PlayerGameStat.targets).label("prior_targets"),
            func.avg(PlayerGameStat.receptions).label("prior_receptions"),
            func.avg(PlayerGameStat.gl_carries).label("prior_gl_carries"),
            func.avg(PlayerGameStat.rz_targets).label("prior_rz_targets"),
            func.avg(PlayerGameStat.rush_td + PlayerGameStat.rec_td).label("prior_td_rate"),
            func.avg(PlayerGameStat.rush_td).label("prior_rush_td"),
            func.avg(PlayerGameStat.rec_td).label("prior_rec_td"),
            func.count().label("n_games"),
        )
        .join(Game, Game.game_id == PlayerGameStat.game_id)
        .filter(
            Game.league_id == league_id,
            Game.status == "final",
            Game.data_tier == "historical",
            PlayerGameStat.player_id.in_(list(player_ids)),
        )
        .group_by(PlayerGameStat.player_id)
    )
    rows = q.all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r._mapping) for r in rows])


def _availability(ep: EligiblePlayer, injury: dict[str, str] | None) -> float:
    if ep.eligibility == "confirmed_ineligible":
        return 0.0
    designation = (injury or {}).get(ep.player_id, "").lower()
    if designation in {"out", "ir", "injured reserve", "pup"}:
        return 0.05
    if designation in {"doubtful"}:
        return 0.25
    if designation in {"questionable"}:
        return 0.75
    if ep.eligibility == "uncertain":
        return 0.85
    return 0.98


def build_opportunities(
    session: Session,
    game: Game,
    team_contexts: dict[str, TeamScoringContext],
    *,
    injuries: dict[str, str] | None = None,
) -> list[PlayerOpportunity]:
    """Eligible skill players with rush/rec allocation weights (team-normalized)."""
    eligible = eligible_players_for_game(session, game)
    skill = [e for e in eligible if e.position_group in SKILL and e.eligibility != "confirmed_ineligible"]
    m6 = load_m6(game.league_id)
    weights = default_weights(game.league_id)
    other = float(weights.other_share or DEFAULT_OTHER_SHARE)
    priors = player_history_priors(session, game.league_id, {e.player_id for e in skill})
    prior_map = {r["player_id"]: r for r in priors.to_dict(orient="records")} if not priors.empty else {}

    raw: list[PlayerOpportunity] = []
    for ep in skill:
        pr = prior_map.get(ep.player_id, {})
        prior_td = float(pr.get("prior_td_rate") or 0.0)
        prior_carries = float(pr.get("prior_carries") or 0.0)
        prior_targets = float(pr.get("prior_targets") or 0.0)
        prior_receptions = float(pr.get("prior_receptions") or 0.0)
        prior_gl = float(pr.get("prior_gl_carries") or 0.0)
        prior_rz_t = float(pr.get("prior_rz_targets") or 0.0)
        prior_rush_td = float(pr.get("prior_rush_td") or 0.0)
        prior_rec_td = float(pr.get("prior_rec_td") or 0.0)
        notes: list[str] = []
        if game.league_id == "fbs":
            prior_gl = 0.0
            prior_rz_t = 0.0
            # Do not invent targets; receptions are the verified receiving volume.
            prior_targets = prior_receptions
            notes.append("fbs_no_fabricated_rz_gl")

        m6_p = m6_mean = None
        if m6:
            m6_p, m6_mean = _m6_predict_simple(m6, ep.player_id, ep.position_group)
            notes.append("m6_rate")
        else:
            notes.append("m6_missing_fallback_priors")

        pos_rush = (weights.pos_rush or {}).get(ep.position_group, 0.3)
        pos_rec = (weights.pos_rec or {}).get(ep.position_group, 0.3)

        if ep.position_group == "QB":
            rush_score = pos_rush * (
                weights.rush_carry * prior_carries
                + weights.rush_gl * prior_gl
                + weights.rush_td_hist * prior_rush_td
            )
            rec_score = 0.0
            notes.append("qb_rush_only_pass_td_to_receiver")
        elif ep.position_group == "RB":
            rush_score = pos_rush * (
                weights.rush_carry * prior_carries
                + weights.rush_gl * prior_gl
                + weights.rush_td_hist * prior_rush_td
            )
            rec_score = pos_rec * (
                weights.rec_target * prior_targets
                + weights.rec_rz * prior_rz_t
                + weights.rec_td_hist * prior_rec_td
            )
        else:
            rush_score = pos_rush * (
                weights.rush_carry * prior_carries * (0.5 if ep.position_group == "WR" else 1.0)
                + weights.rush_gl * prior_gl
                + weights.rush_td_hist * prior_rush_td
            )
            if game.league_id == "fbs":
                rec_score = pos_rec * (1.0 * prior_receptions + weights.rec_td_hist * prior_rec_td)
            else:
                rec_score = pos_rec * (
                    weights.rec_target * prior_targets
                    + weights.rec_rz * prior_rz_t
                    + weights.rec_td_hist * prior_rec_td
                )

        if m6_mean is not None:
            rush_blend = 1.2 if ep.position_group in {"RB", "QB"} else 0.35
            rec_blend = 0.0 if ep.position_group == "QB" else 1.0
            rush_score = 0.7 * rush_score + 0.3 * (m6_mean * rush_blend)
            rec_score = 0.7 * rec_score + 0.3 * (m6_mean * rec_blend)

        # Mild team-context tilt: higher team rush λ → slightly prefer rush scorers on that side.
        ctx = team_contexts.get(ep.team_id)
        if ctx and ctx.expected_off_tds and ctx.expected_off_tds > 0:
            rush_frac = (ctx.expected_rush_tds or 0.0) / ctx.expected_off_tds
            rush_score *= 0.85 + 0.3 * rush_frac
            rec_score *= 0.85 + 0.3 * (1.0 - rush_frac)
            notes.append("team_scoring_context_tilt")

        snap = 0.2
        if m6 and ep.player_id in (m6.get("player_n") or {}):
            snap = min(0.95, 0.15 + 0.02 * float(m6["player_n"][ep.player_id]))
        volume = prior_targets + prior_carries if game.league_id == "nfl" else prior_receptions + prior_carries
        if volume > 0:
            snap = max(snap, min(0.95, volume / 18.0))

        raw.append(
            PlayerOpportunity(
                player_id=ep.player_id,
                team_id=ep.team_id,
                name=ep.name,
                position_group=ep.position_group,
                eligibility=ep.eligibility,
                availability=_availability(ep, injuries),
                rush_weight=max(0.0, rush_score),
                rec_weight=max(0.0, rec_score),
                m6_p=m6_p,
                m6_mean=m6_mean,
                prior_td_rate=prior_td,
                prior_carries=prior_carries,
                prior_targets=prior_targets if game.league_id == "nfl" else prior_receptions,
                prior_gl_carries=prior_gl,
                prior_rz_targets=prior_rz_t,
                snap_share=float(snap),
                notes=notes,
            )
        )

    by_team: dict[str, list[PlayerOpportunity]] = {}
    for row in raw:
        by_team.setdefault(row.team_id, []).append(row)

    normalized: list[PlayerOpportunity] = []
    scale = 1.0 - other
    for _team_id, rows in by_team.items():
        rush_sum = sum(r.rush_weight for r in rows)
        rec_sum = sum(r.rec_weight for r in rows)
        for r in rows:
            rw = scale * (r.rush_weight / rush_sum) if rush_sum > 0 else scale / max(len(rows), 1)
            if r.position_group == "QB":
                cw = 0.0
            elif rec_sum > 0:
                cw = scale * (r.rec_weight / rec_sum)
            else:
                cw = scale / max(len(rows), 1)
            normalized.append(
                PlayerOpportunity(
                    player_id=r.player_id,
                    team_id=r.team_id,
                    name=r.name,
                    position_group=r.position_group,
                    eligibility=r.eligibility,
                    availability=r.availability,
                    rush_weight=float(rw),
                    rec_weight=float(cw),
                    m6_p=r.m6_p,
                    m6_mean=r.m6_mean,
                    prior_td_rate=r.prior_td_rate,
                    prior_carries=r.prior_carries,
                    prior_targets=r.prior_targets,
                    prior_gl_carries=r.prior_gl_carries,
                    prior_rz_targets=r.prior_rz_targets,
                    snap_share=r.snap_share,
                    notes=r.notes + [f"other_share={other}"],
                )
            )
    return normalized


def opportunities_from_frame_rows(rows: pd.DataFrame, league_id: str) -> list[PlayerOpportunity]:
    """Build opportunities from a leakage-safe historical player_td frame slice."""
    weights = default_weights(league_id)
    other = float(weights.other_share or DEFAULT_OTHER_SHARE)
    m6 = load_m6(league_id)
    raw: list[PlayerOpportunity] = []
    for rec in rows.itertuples(index=False):
        pos = str(getattr(rec, "position_group", "WR"))
        pid = str(getattr(rec, "player_id"))
        tid = str(getattr(rec, "team_id"))
        rush_share = float(getattr(rec, "rush_td_share", 0.0) or 0.0)
        rec_share = float(getattr(rec, "rec_td_share", 0.0) or 0.0)
        if pos == "QB":
            rec_share = 0.0
        m6_p = m6_mean = None
        if m6:
            m6_p, m6_mean = _m6_predict_simple(m6, pid, pos)
        avail_raw = getattr(rec, "availability", None)
        if avail_raw is None or isinstance(avail_raw, str):
            # Frame may carry status labels, not probabilities.
            status = str(getattr(rec, "availability_status", avail_raw) or "unknown").lower()
            if status in {"out", "doubtful", "confirmed_inactive"}:
                avail = 0.2
            elif status in {"questionable", "uncertain"}:
                avail = 0.7
            else:
                avail = 1.0
        else:
            try:
                avail = float(avail_raw)
            except (TypeError, ValueError):
                avail = 1.0
        raw.append(
            PlayerOpportunity(
                player_id=pid,
                team_id=tid,
                name=str(getattr(rec, "player_name", pid)),
                position_group=pos,
                eligibility="eligible",
                availability=float(np.clip(avail, 0.0, 1.0)),
                rush_weight=rush_share,
                rec_weight=rec_share,
                m6_p=m6_p,
                m6_mean=m6_mean,
                prior_td_rate=float(getattr(rec, "prior_td_rate", 0.0) or 0.0),
                prior_carries=float(getattr(rec, "prior_carries", 0.0) or 0.0),
                prior_targets=float(getattr(rec, "prior_targets", 0.0) or 0.0),
                prior_gl_carries=float(getattr(rec, "prior_gl_carries", 0.0) or 0.0) if league_id == "nfl" else 0.0,
                prior_rz_targets=float(getattr(rec, "prior_rz_targets", 0.0) or 0.0) if league_id == "nfl" else 0.0,
                snap_share=float(getattr(rec, "snap_roll3", 0.5) or 0.5),
                notes=["from_player_td_frame", f"other_share={other}"],
            )
        )
    # If shares missing, fall back to uniform with residual.
    if raw and all(r.rush_weight == 0 and r.rec_weight == 0 for r in raw):
        scale = 1.0 - other
        n = len(raw)
        for r in raw:
            r.rush_weight = scale / n
            r.rec_weight = 0.0 if r.position_group == "QB" else scale / n
    return raw
