"""Freeze the live CFB Anytime TD board before kickoff, then grade after finals.

This is the research→live-proof loop:
  historical + current info → board probs → freeze pre-kickoff → settle after finals.

Does not enable Validated Play. Append-only shadows. Existing-data / existing ticks only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc
from spe.db.models import ShadowPrediction
from spe.research.atd_guardrails import finder_payload_path
from spe.research.atd_verify import MODEL_IDS, _write_shadow_row, settled_performance
from spe.simulate.correlated_atd.shadow_atd import _priced_anytime_rows
from spe.shadow.jobs import job_atd_candidates, job_settle
from spe.shadow.prospective_collection import settled_unique_counts

BLEND_FAMILY = "blend_v1_c_recent"
BLEND_MODEL_ID = "blend_v1_c_recent-{league}-anytime_td"
C_RECENT_MODEL_ID = MODEL_IDS["m5_c_recent"]
V1_MODEL_ID = MODEL_IDS["player_td_v1"]


def report_dir() -> Path:
    path = Path("data") / "atd_sim_dev" / "live_weekend"
    path.mkdir(parents=True, exist_ok=True)
    return path


def freeze_path() -> Path:
    return report_dir() / "fbs_board_live_freeze.json"


def status_path() -> Path:
    return report_dir() / "fbs_board_live_proof_status.json"


def _kickoffs(session: Session) -> dict[str, datetime]:
    rows = session.execute(
        text(
            """
            SELECT game_id, kickoff_utc
            FROM games
            WHERE league_id = 'fbs'
            """
        )
    ).mappings().all()
    out: dict[str, datetime] = {}
    for r in rows:
        kick = as_utc(r["kickoff_utc"])
        if kick is not None:
            out[str(r["game_id"])] = kick
    return out


def _load_finder() -> dict[str, Any]:
    path = finder_payload_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_board_freeze(
    session: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Snapshot current FBS board probs for pre-kickoff games only."""
    moment = as_utc(now) or datetime.now(UTC)
    finder = _load_finder()
    kickoffs = _kickoffs(session)
    games_out: list[dict[str, Any]] = []
    n_players = 0
    n_prekick = 0
    n_postkick = 0
    n_demoted = 0

    for g in finder.get("games") or []:
        if str(g.get("league_id") or "") != "fbs":
            continue
        gid = str(g.get("game_id") or "")
        kick = kickoffs.get(gid)
        if kick is None:
            continue
        if kick <= moment:
            n_postkick += 1
            continue
        players: list[dict[str, Any]] = []
        for pl in g.get("players") or []:
            n_players += 1
            probs = pl.get("probabilities") or {}
            supported = pl.get("game_level_supported")
            if supported is False:
                n_demoted += 1
                continue
            primary = probs.get("primary_research")
            c_recent = probs.get("c_recent")
            v1 = probs.get("player_td_v1")
            if primary is None and c_recent is None and v1 is None:
                continue
            # Prefer explicit blend; else recompute when both components exist.
            if primary is None and c_recent is not None and v1 is not None:
                primary = 0.60 * float(v1) + 0.40 * float(c_recent)
            if primary is not None:
                try:
                    from spe.shadow.atd_role_calibration import apply_role_calibration

                    feats = pl.get("key_features") or {}
                    carries = feats.get("prior_carries", feats.get("exp_carries"))
                    primary = apply_role_calibration(
                        float(primary),
                        position_group=str(pl.get("position_group") or ""),
                        prior_carries=None if carries is None else float(carries),
                    )
                except Exception:  # noqa: BLE001
                    pass
            players.append(
                {
                    "player_id": pl.get("player_id"),
                    "player_name": pl.get("player_name"),
                    "team_id": pl.get("team_id"),
                    "position_group": pl.get("position_group"),
                    "game_level_supported": True,
                    "p_blend": float(primary) if primary is not None else None,
                    "p_c_recent": float(c_recent) if c_recent is not None else None,
                    "p_player_td_v1": float(v1) if v1 is not None else None,
                    "sportsbook": pl.get("sportsbook"),
                }
            )
            n_prekick += 1
        if players:
            games_out.append(
                {
                    "league_id": "fbs",
                    "game_id": gid,
                    "kickoff_utc": kick.isoformat(),
                    "matchup": f"{g.get('away')} @ {g.get('home')}",
                    "holdout_tag": "fbs_board_live_proof_v1",
                    "players": players,
                }
            )

    freeze = {
        "freeze_id": f"fbs_board_live_{moment.strftime('%Y%m%dT%H%M%SZ')}",
        "frozen_at": moment.isoformat(),
        "source": "anytime_td_finder_board",
        "board_primary": BLEND_FAMILY,
        "play_enabled": False,
        "validated_play": False,
        "research_only": True,
        "n_games": len(games_out),
        "n_prekick_players": n_prekick,
        "n_finder_players_seen": n_players,
        "n_games_already_kicked": n_postkick,
        "n_demoted_skipped": n_demoted,
        "games": games_out,
    }
    freeze_path().write_text(json.dumps(freeze, indent=2, default=str), encoding="utf-8")
    return freeze


def ingest_board_freeze_into_shadow(
    session: Session,
    *,
    freeze: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append-only shadow rows for blend + C_recent + player_td_v1 beside live ticks."""
    moment = as_utc(now) or datetime.now(UTC)
    freeze = freeze or (
        json.loads(freeze_path().read_text(encoding="utf-8")) if freeze_path().exists() else None
    )
    if not freeze:
        return {"status": "missing_freeze", "written": 0}

    priced = _priced_anytime_rows(session)
    if not priced:
        return {"status": "no_priced_anytime_td", "written": 0}

    # Prefer latest tick per (game, player).
    by_gp: dict[tuple[str, str], dict[str, Any]] = {}
    for row in priced.values():
        key = (str(row["game_id"]), str(row["player_id"]))
        prev = by_gp.get(key)
        if prev is None:
            by_gp[key] = row
            continue
        prev_cap = as_utc(prev.get("captured_at")) or datetime.min.replace(tzinfo=UTC)
        cur_cap = as_utc(row.get("captured_at")) or datetime.min.replace(tzinfo=UTC)
        if cur_cap >= prev_cap:
            by_gp[key] = row

    existing = {
        (r.outcome_id, r.model_version_id, r.odds_tick_id)
        for r in session.query(ShadowPrediction)
        .filter(ShadowPrediction.market_id == "anytime_td")
        .filter(ShadowPrediction.league_id == "fbs")
        .all()
    }

    summary: dict[str, Any] = {
        "status": "ok",
        "written": 0,
        "unchanged": 0,
        "skipped_no_price": 0,
        "skipped_no_prob": 0,
        "skipped_post_kickoff": 0,
        "by_model": {},
        "play_enabled": False,
        "validated_play": False,
        "research_only": True,
        "freeze_id": freeze.get("freeze_id"),
        "freeze_frozen_at": freeze.get("frozen_at"),
    }

    kickoffs = _kickoffs(session)
    for g in freeze.get("games") or []:
        gid = str(g.get("game_id") or "")
        kick = kickoffs.get(gid)
        if kick is not None and kick <= moment:
            summary["skipped_post_kickoff"] += len(g.get("players") or [])
            continue
        for pl in g.get("players") or []:
            pid = str(pl.get("player_id") or "")
            if not pid:
                continue
            price_row = by_gp.get((gid, pid))
            if price_row is None:
                summary["skipped_no_price"] += 1
                continue

            candidates: list[tuple[str, float | None, dict[str, Any]]] = [
                (
                    BLEND_MODEL_ID.format(league="fbs"),
                    pl.get("p_blend"),
                    {
                        "family": BLEND_FAMILY,
                        "primary": True,
                        "board_primary": True,
                        "composed_of": ["player_td_v1", "m5_c_recent"],
                        "blend_weights": {"player_td_v1": 0.55, "m5_c_recent": 0.45},
                        "holdout_tag": g.get("holdout_tag"),
                        "matchup": g.get("matchup"),
                        "player_name": pl.get("player_name"),
                        "position_group": pl.get("position_group"),
                        "fbs_receptions_usage_proxy": True,
                    },
                ),
                (
                    C_RECENT_MODEL_ID.format(league="fbs"),
                    pl.get("p_c_recent"),
                    {
                        "family": "m5_c_recent",
                        "holdout_tag": g.get("holdout_tag"),
                        "matchup": g.get("matchup"),
                        "player_name": pl.get("player_name"),
                        "position_group": pl.get("position_group"),
                        "fbs_receptions_usage_proxy": True,
                    },
                ),
                (
                    V1_MODEL_ID.format(league="fbs"),
                    pl.get("p_player_td_v1"),
                    {
                        "family": "player_td_v1",
                        "holdout_tag": g.get("holdout_tag"),
                        "matchup": g.get("matchup"),
                        "player_name": pl.get("player_name"),
                        "position_group": pl.get("position_group"),
                        "fbs_receptions_usage_proxy": True,
                    },
                ),
            ]

            for model_version_id, p, extra in candidates:
                if p is None:
                    summary["skipped_no_prob"] += 1
                    continue
                key = (price_row["outcome_id"], model_version_id, price_row["tick_id"])
                if key in existing:
                    summary["unchanged"] += 1
                    continue
                _write_shadow_row(
                    session,
                    price_row=price_row,
                    model_version_id=model_version_id,
                    p=float(p),
                    now=moment,
                    freeze_meta={
                        "freeze_frozen_at": freeze.get("frozen_at"),
                        "freeze_id": freeze.get("freeze_id"),
                        **extra,
                    },
                )
                existing.add(key)
                summary["written"] += 1
                fam = str(extra.get("family"))
                summary["by_model"][fam] = int(summary["by_model"].get(fam, 0)) + 1

    session.flush()
    return summary


def board_live_proof_snapshot(session: Session) -> dict[str, Any]:
    """Counts for the live-proof ledger (pre-kickoff freezes + settled W/L)."""
    open_rows = session.execute(
        text(
            """
            SELECT
              CASE
                WHEN s.model_version_id LIKE 'blend_v1_c_recent%' THEN 'blend_v1_c_recent'
                WHEN s.model_version_id LIKE 'm5_c_recent%' THEN 'm5_c_recent'
                WHEN s.model_version_id LIKE 'player_td_v1%' THEN 'player_td_v1'
                ELSE 'other'
              END AS family,
              COUNT(*) AS n,
              COUNT(DISTINCT s.game_id) AS n_games,
              COUNT(DISTINCT s.player_id||'|'||s.game_id) AS n_player_games
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            LEFT JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            WHERE s.league_id = 'fbs'
              AND s.market_id = 'anytime_td'
              AND s.forecast_as_of < g.kickoff_utc
              AND (
                s.model_version_id LIKE 'blend_v1_c_recent%'
                OR s.model_version_id LIKE 'm5_c_recent%'
                OR s.model_version_id LIKE 'player_td_v1%'
              )
              AND st.shadow_id IS NULL
            GROUP BY 1
            """
        )
    ).mappings().all()
    settled_rows = session.execute(
        text(
            """
            SELECT
              CASE
                WHEN s.model_version_id LIKE 'blend_v1_c_recent%' THEN 'blend_v1_c_recent'
                WHEN s.model_version_id LIKE 'm5_c_recent%' THEN 'm5_c_recent'
                WHEN s.model_version_id LIKE 'player_td_v1%' THEN 'player_td_v1'
                ELSE 'other'
              END AS family,
              COUNT(*) AS n,
              SUM(CASE WHEN st.result='win' THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN st.result='loss' THEN 1 ELSE 0 END) AS losses,
              COUNT(DISTINCT s.game_id) AS n_games
            FROM shadow_predictions s
            JOIN shadow_settlements st ON st.shadow_id = s.shadow_id
            JOIN games g ON g.game_id = s.game_id
            WHERE s.league_id = 'fbs'
              AND s.market_id = 'anytime_td'
              AND s.forecast_as_of < g.kickoff_utc
              AND st.result IN ('win','loss')
              AND (
                s.model_version_id LIKE 'blend_v1_c_recent%'
                OR s.model_version_id LIKE 'm5_c_recent%'
                OR s.model_version_id LIKE 'player_td_v1%'
              )
            GROUP BY 1
            """
        )
    ).mappings().all()
    return {
        "open_prekickoff_freezes": [dict(r) for r in open_rows],
        "settled_win_loss": [dict(r) for r in settled_rows],
        "unique_settled": settled_unique_counts(session),
        "settled_performance": settled_performance(session),
    }


def run_fbs_board_live_proof(
    session: Session,
    *,
    now: datetime | None = None,
    also_capture_legacy_candidates: bool = True,
    refresh_scoreboard: bool = True,
) -> dict[str, Any]:
    """Full loop: freeze board → shadow ingest → settle finals → status report.

    Validated Play stays off. This only builds the moving-forward report card.
    """
    moment = as_utc(now) or datetime.now(UTC)
    report: dict[str, Any] = {
        "title": "CFB Anytime TD live proof loop",
        "generated_at": moment.isoformat(),
        "play_enabled": False,
        "validated_play": False,
        "research_only": True,
        "loop": [
            "historical + current board info",
            "freeze pre-kickoff board probs",
            "settle after finals",
            "accumulate live proof (Play stays off until bar clears)",
        ],
    }

    if refresh_scoreboard:
        print("[cfb-live-proof] refreshing scoreboard statuses…", flush=True)
        try:
            from spe.ingest.espn_scoreboard import ingest_football_scoreboard

            report["scoreboard"] = ingest_football_scoreboard(session, year=moment.year)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            report["scoreboard"] = {"status": "error", "error": str(exc)}

    if also_capture_legacy_candidates:
        print("[cfb-live-proof] legacy ATD candidate freeze (m3/m5/m6/sim)…", flush=True)
        report["legacy_candidates"] = job_atd_candidates(session)
        session.commit()

    print("[cfb-live-proof] building board freeze from finder…", flush=True)
    freeze = build_board_freeze(session, now=moment)
    report["freeze"] = {
        "freeze_id": freeze.get("freeze_id"),
        "n_games": freeze.get("n_games"),
        "n_prekick_players": freeze.get("n_prekick_players"),
        "n_games_already_kicked": freeze.get("n_games_already_kicked"),
        "n_demoted_skipped": freeze.get("n_demoted_skipped"),
        "path": str(freeze_path()),
    }

    print("[cfb-live-proof] ingesting blend / C_recent / v1 into shadow…", flush=True)
    ingest = ingest_board_freeze_into_shadow(session, freeze=freeze, now=moment)
    session.commit()
    report["shadow_ingest"] = ingest

    print("[cfb-live-proof] settling finished games…", flush=True)
    settle = job_settle(session)
    session.commit()
    report["settle"] = {
        "status": settle.get("status"),
        "summary": settle.get("summary"),
        "error": settle.get("error"),
    }

    report["live_proof"] = board_live_proof_snapshot(session)
    report["next"] = {
        "keep_running": "Re-run cfb-atd-live-proof before each slate; settle after finals",
        "play_unlock": "Only after settled blend sample clears the pre-registered accuracy bar",
        "validated_play": False,
    }

    path = status_path()
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report
