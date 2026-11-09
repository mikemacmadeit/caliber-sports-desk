"""Overlay player-specific FBS Anytime TD research scores onto the props board.

The legacy M3 hierarchical publisher collapses unknown players to the same team
prior, so whole depth charts show identical percentages. Prefer locked C_recent
(or demote/hide) instead of those clones. Research-only; Validated Play stays off.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from spe.research.atd_guardrails import finder_payload_path

_CACHE: dict[str, Any] = {"mtime": None, "loaded_at": 0.0, "index": {}}
_CACHE_TTL_SEC = 30.0


def _load_index(path: Path | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    target = path or finder_payload_path()
    try:
        mtime = target.stat().st_mtime if target.exists() else None
    except OSError:
        mtime = None
    now = time.time()
    if (
        _CACHE["index"]
        and _CACHE["mtime"] == mtime
        and (now - float(_CACHE["loaded_at"])) < _CACHE_TTL_SEC
    ):
        return _CACHE["index"]
    if mtime is None or not target.exists():
        _CACHE.update({"mtime": None, "loaded_at": now, "index": {}})
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_CACHE["index"] or {})
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for game in payload.get("games") or []:
        if str(game.get("league_id") or "") != "fbs":
            continue
        gid = str(game.get("game_id") or "")
        for pl in game.get("players") or []:
            pid = str(pl.get("player_id") or "")
            if gid and pid:
                index[(gid, pid)] = pl
    _CACHE.update({"mtime": mtime, "loaded_at": now, "index": index})
    return index


def is_m3_team_clone_forecast(forecast_payload: dict[str, Any] | None) -> bool:
    """Heuristic: official M3 FBS ATD rows are the clone source on the props board."""
    if not forecast_payload:
        return False
    version = str(forecast_payload.get("model_version") or "")
    return version.startswith("m3-fbs-anytime_td") or version == "m3-fbs-anytime_td"


def apply_fbs_atd_research_overlay(
    forecast_payload: dict[str, Any],
    *,
    game_id: str,
    player_id: str | None,
    league_id: str | None,
    market_id: str | None,
) -> dict[str, Any] | None:
    """Replace M3 team-prior clones with player-specific research scores.

    Returns:
      - updated payload when a research primary exists
      - None when the only available score is an M3 clone / demoted-without-primary
        (caller should hide the row rather than show a fake shared %)
      - original payload for non-FBS / non-ATD markets
    """
    if str(league_id or "") != "fbs" or str(market_id or "") != "anytime_td":
        return forecast_payload
    if not player_id:
        return forecast_payload

    hit = _load_index().get((str(game_id), str(player_id)))
    out = dict(forecast_payload)
    reasons = list(out.get("reason_codes") or [])

    if hit is None:
        # No player-specific research score — never keep an M3 depth-chart clone.
        if is_m3_team_clone_forecast(out):
            return None
        return out

    probs = hit.get("probabilities") or {}
    primary = probs.get("primary_research")
    raw = probs.get("c_recent_raw_involved_conditional")
    v1 = probs.get("player_td_v1")
    c_recent = probs.get("c_recent") if probs.get("c_recent") is not None else primary
    ms = hit.get("model_source") or {}
    game_level = hit.get("game_level_supported")

    if primary is None and (v1 is None or game_level is False):
        # Demoted / not game-level — do not fall back to M3 team clone on the board.
        if is_m3_team_clone_forecast(out) or raw is not None:
            return None
        return out

    # Historical FBS OOS favors player_td_v1; blend with C_recent when both exist
    # and the row is game-level supported. Still research-only.
    source = "c_recent"
    if v1 is not None and c_recent is not None and game_level is not False:
        p = 0.60 * float(v1) + 0.40 * float(c_recent)
        source = "blend_player_td_v1_c_recent"
        version = "fbs_research_blend_v1_c_recent"
    elif v1 is not None and (primary is None or game_level is False):
        # Prefer v1 over a missing/demoted C_recent primary when v1 exists.
        p = float(v1)
        source = "player_td_v1"
        version = "player_td_v1"
    elif primary is not None:
        p = float(primary)
        source = "c_recent"
        version = str(ms.get("locked_version") or ms.get("primary") or "m5_c_recent")
    else:
        return None

    # Availability: never show Out/Doubtful as a live research opportunity.
    avail = str(hit.get("availability_status") or out.get("availability_status") or "").lower()
    if avail in {"out", "doubtful", "confirmed_inactive"}:
        return None

    # Role calibration: lift underconfident rushing QBs / high-carry RBs.
    try:
        from spe.shadow.atd_role_calibration import apply_role_calibration

        carries = hit.get("prior_carries")
        if carries is None:
            carries = (hit.get("key_features") or {}).get("prior_carries")
        if carries is None:
            carries = (hit.get("key_features") or {}).get("exp_carries")
        p_fixed = apply_role_calibration(
            float(p),
            position_group=str(hit.get("position_group") or hit.get("position") or ""),
            prior_carries=None if carries is None else float(carries),
        )
        if abs(float(p_fixed) - float(p)) > 1e-9:
            out["p_estimate_pre_role_calib"] = float(p)
            p = float(p_fixed)
            if "ROLE_CALIBRATION_V1" not in reasons:
                reasons.append("ROLE_CALIBRATION_V1")
    except Exception:  # noqa: BLE001
        pass

    # Do not invent the old M3 ±8pp costume; show a point estimate.
    out["p_estimate"] = p
    out["confidence_interval"] = (p, p)
    out["model_version"] = version
    out["prediction_as_of"] = ms.get("prediction_timestamp") or out.get("prediction_as_of")
    out["data_cutoff"] = ms.get("prediction_timestamp") or out.get("data_cutoff")
    out["data_tier"] = "research"
    out["research_only"] = True
    out["validated_play"] = False
    out["play_enabled"] = False
    out["recommendation_status"] = "no_bet"
    if avail and avail not in {"unknown", "missing_report"}:
        out["availability_status"] = avail
    for code in (
        "RESEARCH_ONLY",
        "PLAY_DISABLED",
        "FBS_PLAYER_SPECIFIC_OVERRIDE",
        "M3_TEAM_PRIOR_OVERRIDE",
        f"FBS_SOURCE_{source.upper()}",
    ):
        if code not in reasons:
            reasons.append(code)
    out["reason_codes"] = reasons
    out["disclaimer"] = (
        "FBS Anytime TD uses player-specific research scores "
        "(player_td_v1 / C_recent blend when both available), not M3 team-prior clones. "
        "Not a validated betting recommendation."
    )
    return out
