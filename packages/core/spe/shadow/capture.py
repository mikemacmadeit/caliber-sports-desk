"""Capture prospective M3 and frozen M4 forecasts at live sportsbook prices."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from spe.db.base import as_utc, new_id
from spe.db.models import Forecast, Game, OddsTick, Outcome, Player, ShadowPrediction
from spe.market.odds import american_to_implied, ev_per_dollar, no_vig_two_way
from spe.market.price_age import max_fresh_minutes
from spe.recommend.engine import RecommendInput, recommend
from spe.shadow.m4_artifact import adjust_mean, load_m4_artifacts, probability_from_mean

FAMILY = {"m3": "m3", "m4": "m4"}


def capture_shadow_predictions(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """Append shadow rows for the latest research forecast on each priced outcome.

    Does not rewrite forecasts or odds ticks. Play stays disabled.
    """
    now = now or datetime.now(UTC)
    summary: dict[str, Any] = {
        "written": 0,
        "unchanged": 0,
        "skipped": 0,
        "m3": 0,
        "m4": 0,
        "play_enabled": False,
        "research_only": True,
        "abstain_reason_counts": {},
    }
    rows = session.execute(
        text(
            """
            SELECT o.outcome_id, o.game_id, o.player_id, o.market_id, o.side, o.line, o.description,
                   g.league_id, g.status, g.kickoff_utc, g.week,
                   f.forecast_id, f.model_version_id, f.p, f.p_low, f.p_high, f.as_of,
                   f.availability_status, f.reason_codes, f.components, f.data_tier,
                   t.tick_id, t.bookmaker_id, t.price_american, t.captured_at,
                   pl.position_group, pl.name
            FROM outcomes o
            JOIN games g ON g.game_id = o.game_id
            JOIN forecasts f ON f.outcome_id = o.outcome_id
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            LEFT JOIN players pl ON pl.player_id = o.player_id
            WHERE g.data_tier = 'upcoming'
              AND g.status IN ('scheduled', 'preview', 'pregame')
              AND g.league_id IN ('nfl', 'fbs')
              AND o.market_id IN ('anytime_td', 'receptions', 'receiving_yards', 'rushing_yards')
              AND f.is_official = 1
              AND f.data_tier = 'research'
              AND f.model_version_id LIKE 'm3-%'
              AND o.player_id IS NOT NULL
            """
        )
    ).mappings().all()

    existing = {
        (
            r.outcome_id,
            r.model_version_id,
            r.odds_tick_id,
            (r.components or {}).get("source_forecast_id") if isinstance(r.components, dict) else None,
        )
        for r in session.query(ShadowPrediction).all()
    }
    m4 = {league: load_m4_artifacts(league) for league in ("nfl", "fbs")}

    if rows:
        latest_forecast: dict[str, Any] = {}
        latest_tick: dict[str, Any] = {}
        for row in rows:
            oid = row["outcome_id"]
            prev_f = latest_forecast.get(oid)
            if prev_f is None or _as_dt(row["as_of"]) >= _as_dt(prev_f["as_of"]):
                latest_forecast[oid] = row
            prev_t = latest_tick.get(oid)
            if prev_t is None or _as_dt(row["captured_at"]) >= _as_dt(prev_t["captured_at"]):
                latest_tick[oid] = row

        feature_cache = _usage_features(session, {row["game_id"] for row in latest_forecast.values()})

        for oid, row in latest_forecast.items():
            tick = latest_tick.get(oid)
            if tick is None:
                summary["skipped"] += 1
                continue
            kickoff_raw = row["kickoff_utc"]
            if isinstance(kickoff_raw, str):
                kickoff_raw = datetime.fromisoformat(kickoff_raw.replace("Z", "+00:00"))
            kickoff = as_utc(kickoff_raw)
            if kickoff is not None and kickoff <= now:
                summary["skipped"] += 1
                continue
            m3_version = row["model_version_id"]
            source_fid = row["forecast_id"]
            if (oid, m3_version, tick["tick_id"], source_fid) in existing:
                summary["unchanged"] += 1
            else:
                written = _write_row(
                    session,
                    row=row,
                    tick=tick,
                    model_version_id=m3_version,
                    p=float(row["p"]),
                    mean=_json_obj(row["components"]).get("mean"),
                    family="m3",
                    now=now,
                    extra_reasons=[],
                    feature_notes=[],
                )
                summary["written"] += 1
                summary["m3"] += 1
                _tally(summary, written["abstain_reasons"])
                existing.add((oid, m3_version, tick["tick_id"], source_fid))

            m4_version = f"m4-{row['league_id']}-{row['market_id']}"
            if (oid, m4_version, tick["tick_id"], source_fid) in existing:
                continue
            market = (m4.get(row["league_id"]) or {}).get("markets", {}).get(row["market_id"])
            if not market:
                continue
            mean = float(_json_obj(row["components"]).get("mean") or 0.0)
            feats = feature_cache.get((row["player_id"], row["game_id"]), {})
            adjusted, missing = adjust_mean(mean, feats, market)
            p_m4 = probability_from_mean(adjusted, market, line=row["line"], side=row["side"])
            notes = [f"M4_FEATURE_MISSING:{name}" for name in missing]
            written = _write_row(
                session,
                row=row,
                tick=tick,
                model_version_id=m4_version,
                p=p_m4,
                mean=adjusted,
                family="m4",
                now=now,
                extra_reasons=notes,
                feature_notes=missing,
                components_extra={
                    "features_used": [k for k in (market.get("features") or []) if k not in missing],
                    "missing_features": missing,
                },
            )
            summary["written"] += 1
            summary["m4"] += 1
            _tally(summary, written["abstain_reasons"])
            existing.add((oid, m4_version, tick["tick_id"], source_fid))
    else:
        summary["official_query"] = "no_priced_official_forecasts"

    # Pregame M4 backfill: priced outcomes that already have an M3 shadow but no M4
    # (e.g. unofficial research m3 freeze written earlier; official query skips them).
    backfill = _backfill_m4_from_m3_shadows(session, now=now, m4_artifacts=m4, existing=existing)
    summary["m4_backfill"] = backfill
    summary["written"] += int(backfill.get("written") or 0)
    summary["m4"] += int(backfill.get("written") or 0)
    session.flush()
    summary["status"] = "ok"
    return summary


def _backfill_m4_from_m3_shadows(
    session: Session,
    *,
    now: datetime,
    m4_artifacts: dict[str, Any],
    existing: set[tuple],
) -> dict[str, Any]:
    """Append M4 for pregame priced ATD rows that already have M3 but lack M4."""
    out: dict[str, Any] = {"written": 0, "candidates": 0, "skipped_no_market": 0, "skipped_kickoff": 0}
    gaps = session.execute(
        text(
            """
            SELECT s.shadow_id, s.outcome_id, s.game_id, s.player_id, s.market_id, s.side, s.line,
                   s.league_id, s.odds_tick_id, s.bookmaker_id, s.price_american, s.forecast_probability,
                   s.components, s.forecast_as_of,
                   g.kickoff_utc, g.status,
                   pl.position_group, pl.name,
                   t.tick_id, t.bookmaker_id AS tick_book, t.price_american AS tick_price, t.captured_at,
                   f.forecast_id, f.p AS forecast_p, f.components AS forecast_components,
                   f.availability_status, f.reason_codes
            FROM shadow_predictions s
            JOIN games g ON g.game_id = s.game_id
            JOIN odds_ticks t ON t.tick_id = s.odds_tick_id
            LEFT JOIN players pl ON pl.player_id = s.player_id
            LEFT JOIN forecasts f ON f.outcome_id = s.outcome_id
              AND f.model_version_id LIKE 'm3-%'
              AND f.data_tier = 'research'
            WHERE s.model_version_id LIKE 'm3-%'
              AND s.market_id = 'anytime_td'
              AND g.data_tier = 'upcoming'
              AND g.status IN ('scheduled', 'preview', 'pregame')
              AND g.kickoff_utc > :now
              AND NOT EXISTS (
                SELECT 1 FROM shadow_predictions s2
                WHERE s2.outcome_id = s.outcome_id
                  AND s2.model_version_id LIKE 'm4-%'
                  AND s2.forecast_as_of < g.kickoff_utc
              )
            """
        ),
        {"now": now.isoformat()},
    ).mappings().all()
    out["candidates"] = len(gaps)
    if not gaps:
        return out

    feature_cache = _usage_features(session, {row["game_id"] for row in gaps})
    for row in gaps:
        kickoff = as_utc(row["kickoff_utc"])
        if kickoff is not None and kickoff <= now:
            out["skipped_kickoff"] += 1
            continue
        league = str(row["league_id"])
        market = (m4_artifacts.get(league) or {}).get("markets", {}).get(row["market_id"])
        if not market:
            out["skipped_no_market"] += 1
            continue
        m4_version = f"m4-{league}-{row['market_id']}"
        source_fid = row["forecast_id"] or _json_obj(row["components"]).get("source_forecast_id")
        tick_id = row["tick_id"] or row["odds_tick_id"]
        key = (row["outcome_id"], m4_version, tick_id, source_fid)
        if key in existing:
            continue
        # Prefer forecast mean; else m3 shadow mean; else invert from m3 p.
        mean = _json_obj(row["forecast_components"]).get("mean")
        if mean is None:
            mean = _json_obj(row["components"]).get("mean")
        if mean is None:
            mean = float(row["forecast_probability"] or 0.0)
        mean = float(mean or 0.0)
        feats = feature_cache.get((row["player_id"], row["game_id"]), {})
        adjusted, missing = adjust_mean(mean, feats, market)
        p_m4 = probability_from_mean(adjusted, market, line=row["line"], side=row["side"])
        notes = [f"M4_FEATURE_MISSING:{name}" for name in missing]
        notes.append("M4_BACKFILL_FROM_EXISTING_M3_SHADOW")
        write_row = {
            "outcome_id": row["outcome_id"],
            "game_id": row["game_id"],
            "player_id": row["player_id"],
            "market_id": row["market_id"],
            "side": row["side"],
            "line": row["line"],
            "league_id": league,
            "status": row["status"],
            "kickoff_utc": row["kickoff_utc"],
            "forecast_id": source_fid,
            "availability_status": row["availability_status"] or "unknown",
            "reason_codes": row["reason_codes"] or [],
            "components": row["forecast_components"] or row["components"],
            "position_group": row["position_group"],
            "name": row["name"],
        }
        tick = {
            "tick_id": tick_id,
            "bookmaker_id": row["tick_book"] or row["bookmaker_id"],
            "price_american": row["tick_price"] if row["tick_price"] is not None else row["price_american"],
            "captured_at": row["captured_at"],
        }
        written = _write_row(
            session,
            row=write_row,
            tick=tick,
            model_version_id=m4_version,
            p=p_m4,
            mean=adjusted,
            family="m4",
            now=now,
            extra_reasons=notes,
            feature_notes=missing,
            components_extra={
                "features_used": [k for k in (market.get("features") or []) if k not in missing],
                "missing_features": missing,
                "backfill_from_m3_shadow_id": row["shadow_id"],
            },
        )
        out["written"] += 1
        existing.add(key)
    return out


def _write_row(
    session: Session,
    *,
    row: Any,
    tick: Any,
    model_version_id: str,
    p: float,
    mean: Any,
    family: str,
    now: datetime,
    extra_reasons: list[str],
    feature_notes: list[str],
    components_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    price = int(tick["price_american"])
    implied_raw = american_to_implied(price)
    opposing = _opposing_price(session, row["outcome_id"], tick["bookmaker_id"], row["side"])
    novig = None
    if opposing is not None:
        try:
            a, _b = no_vig_two_way(implied_raw, american_to_implied(opposing))
            novig = float(a)
        except ValueError:
            novig = None
    result = recommend(
        RecommendInput(
            p=p,
            p_low=max(1e-6, p - 0.08),
            p_high=min(1.0, p + 0.08),
            american=price,
            price_captured_at=_as_dt(tick["captured_at"]),
            game_status=row["status"] if row["status"] in {"scheduled", "preview", "pregame"} else "scheduled",
            availability_status=row["availability_status"] or "unknown",
            availability_prob=_availability_prob(row["availability_status"]),
            injury_confidence=None,
            source_fresh=True,
            grading_supported=True,
            calibration_n=0,
            max_price_age_minutes=max_fresh_minutes(_as_dt(row.get("kickoff_utc")), now),
        ),
        now=now,
    )
    forecast_reasons = list(_json_list(row["reason_codes"]))
    reasons = _dedupe(
        [
            "RESEARCH_ONLY",
            "PLAY_DISABLED",
            "SHADOW_TRACK",
            *forecast_reasons,
            *result.reason_codes,
            *extra_reasons,
        ]
    )
    specific_injury = {
        "INJURY_REPORT_NOT_YET_SCHEDULED",
        "INJURY_REPORT_EXPECTED_MISSING",
        "INJURY_SOURCE_UNAVAILABLE",
        "CONFIRMED_INACTIVE",
        "UNCERTAIN_PLAYER_AVAILABILITY",
    }
    if specific_injury.intersection(reasons):
        reasons = [r for r in reasons if r != "INJURY_COVERAGE_INCOMPLETE"]
    if float(result.ev or ev_per_dollar(p, price)) <= 0:
        if "NEGATIVE_EXPECTED_VALUE" not in reasons:
            reasons.append("NEGATIVE_EXPECTED_VALUE")
    quality = _quality(row["availability_status"], feature_notes, reasons)
    try:
        from spe.shadow.live_validation import pregame_opportunity_snapshot
        from spe.shadow.freeze_versioning import build_freeze_version_block

        opp_snap = pregame_opportunity_snapshot(session, str(row["player_id"]), str(row["game_id"]))
        version_block = build_freeze_version_block(
            family=family,
            league_id=str(row["league_id"]),
            forecast_as_of=now,
            source_data_cutoff=now,
            price_captured_at=_as_dt(tick["captured_at"]),
            bookmaker_id=str(tick.get("bookmaker_id") or ""),
            opportunity=opp_snap,
        )
    except Exception:  # noqa: BLE001 — never block capture on research metadata
        opp_snap = {}
        version_block = {}
    components = {
        "mean": mean,
        "line": row["line"],
        "side": row["side"],
        "family": family,
        "position_group": row["position_group"],
        "player_name": row["name"],
        "market_implied_raw": implied_raw,
        "market_implied_novig": novig,
        "novig_identified": novig is not None,
        "research_line": False,
        "line_source": "sportsbook",
        "play_enabled": False,
        "source_forecast_id": row["forecast_id"],
        "injury_coverage": _json_obj(row["components"]).get("injury_coverage"),
        "prior_carries": opp_snap.get("prior_carries"),
        "prior_targets": opp_snap.get("prior_targets"),
        "prior_receptions": opp_snap.get("prior_receptions"),
        "prior_atd_rate": opp_snap.get("prior_atd_rate"),
        "n_prior_games": opp_snap.get("n_prior_games"),
        "prior_team_td_share": opp_snap.get("prior_team_td_share"),
        "team_td_share": opp_snap.get("prior_team_td_share"),
        **version_block,
        **(components_extra or {}),
    }
    session.add(
        ShadowPrediction(
            shadow_id=new_id(),
            player_id=row["player_id"],
            game_id=row["game_id"],
            league_id=row["league_id"],
            market_id=row["market_id"],
            outcome_id=row["outcome_id"],
            bookmaker_id=tick["bookmaker_id"],
            line=None if row["line"] is None else float(row["line"]),
            side=row["side"],
            price_american=price,
            odds_tick_id=tick["tick_id"],
            forecast_probability=float(p),
            market_implied_raw=float(implied_raw),
            market_implied_novig=novig,
            expected_value=result.ev,
            model_version_id=model_version_id,
            forecast_as_of=now,
            data_quality_status=quality,
            abstain_reasons=reasons,
            recommendation_status="no_bet",
            components=components,
            research_only=True,
            play_enabled=False,
            created_at=now,
        )
    )
    return {"abstain_reasons": reasons}


def _opposing_price(session: Session, outcome_id: str, bookmaker_id: str, side: str) -> int | None:
    want = {"over": "under", "under": "over", "yes": "no", "no": "yes"}.get(side)
    if not want:
        return None
    row = session.execute(
        text(
            """
            SELECT t.price_american
            FROM outcomes o
            JOIN odds_ticks t ON t.outcome_id = o.outcome_id
            WHERE o.game_id = (SELECT game_id FROM outcomes WHERE outcome_id = :oid)
              AND o.player_id = (SELECT player_id FROM outcomes WHERE outcome_id = :oid)
              AND o.market_id = (SELECT market_id FROM outcomes WHERE outcome_id = :oid)
              AND o.side = :side
              AND (
                (o.line IS NULL AND (SELECT line FROM outcomes WHERE outcome_id = :oid) IS NULL)
                OR abs(coalesce(o.line, 0) - coalesce((SELECT line FROM outcomes WHERE outcome_id = :oid), 0)) < 1e-6
              )
              AND t.bookmaker_id = :book
            ORDER BY t.captured_at DESC
            LIMIT 1
            """
        ),
        {"oid": outcome_id, "side": want, "book": bookmaker_id},
    ).first()
    return int(row[0]) if row else None


def _usage_features(session: Session, game_ids: set[str]) -> dict[tuple[str, str], dict[str, float | None]]:
    if not game_ids:
        return {}
    from sqlalchemy import bindparam

    stmt = text(
        """
        SELECT player_id, game_id,
               json_extract(metrics, '$.offense_snap_share_roll3') AS snap_roll3,
               json_extract(metrics, '$.offense_snap_share_delta_3v10') AS workload_delta,
               json_extract(metrics, '$.depth_rank_prior_week') AS depth_rank,
               json_extract(metrics, '$.starter_prior_week') AS starter
        FROM player_analytics
        WHERE grain = 'usage_pregame' AND game_id IN :games
        """
    ).bindparams(bindparam("games", expanding=True))
    rows = session.execute(stmt, {"games": list(game_ids)}).mappings().all()
    out: dict[tuple[str, str], dict[str, float | None]] = {}
    for row in rows:
        out[(row["player_id"], row["game_id"])] = {
            "snap_roll3": _num(row["snap_roll3"]),
            "workload_delta": _num(row["workload_delta"]),
            "depth_rank": _num(row["depth_rank"]),
            "starter": _num(row["starter"]),
        }
    return out


def _json_obj(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value) if value else {}
    return dict(value)


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return json.loads(value) if value else []
    return list(value)


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return as_utc(value) or value
    return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00"))) or datetime.now(UTC)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _availability_prob(status: str | None) -> float:
    return {"probable": 0.92, "questionable": 0.7, "doubtful": 0.35}.get(str(status or "").lower(), 0.0)


def _quality(availability: str | None, missing_features: list[str], reasons: list[str]) -> str:
    injury_flags = {
        "INJURY_COVERAGE_INCOMPLETE",
        "INJURY_REPORT_NOT_YET_SCHEDULED",
        "INJURY_REPORT_EXPECTED_MISSING",
        "INJURY_SOURCE_UNAVAILABLE",
        "CONFIRMED_INACTIVE",
        "NEVER_ASSUME_HEALTHY",
    }
    if injury_flags.intersection(reasons) or availability in {None, "unknown", "missing_report"}:
        return "injury_unknown"
    if missing_features:
        return "partial_features"
    if "PRICE_STALE" in reasons:
        return "stale_price"
    return "ok"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _tally(summary: dict[str, Any], reasons: list[str]) -> None:
    counts = summary["abstain_reason_counts"]
    for reason in reasons:
        counts[reason] = int(counts.get(reason, 0)) + 1
