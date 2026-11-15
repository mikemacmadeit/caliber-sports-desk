"""Rebuild local research from existing data without requiring a scheduled worker."""
from __future__ import annotations

import hashlib
import logging
import threading
import time

from spe.config import settings

_lock = threading.Lock()
_states = {}


def scoring_signature():
    from spe.models.player_td.desk_opportunity import load_model
    from spe.models.player_td.desk_combined import load_combined_model
    models = {league: (load_model(league) or {}).get("artifact_sha256", "missing")
              for league in ("nfl", "fbs")}
    models.update({league + "_combined": (load_combined_model(league) or {}).get("artifact_sha256", "missing")
                   for league in ("nfl", "fbs")})
    for league in ("nfl", "fbs"):
        models[league + "_situational_candidate"] = (load_combined_model(league, research_candidate=True, version="desk_combined_v5") or {}).get("artifact_sha256", "missing")
        models[league + "_candidate"] = (load_combined_model(league, research_candidate=True) or {}).get("artifact_sha256", "missing")
        evidence = settings.parquet_dir / "desk_evidence" / (league + ".json")
        models[league + "_evidence"] = str(evidence.stat().st_mtime_ns) if evidence.exists() else "missing"
    roster = settings.parquet_dir / "desk_evidence/fbs_rosters.json"
    models["college_rosters"] = str(roster.stat().st_mtime_ns) if roster.exists() else "missing"
    return hashlib.sha256(("desk-local-slate-stays-up-v3:" + repr(sorted(models.items()))).encode()).hexdigest()


def ensure_local_board(db, date, league="all"):
    """Queue one rebuild for a missing, expired or old-model local slate.

    Production remains worker-owned. No network calls, paid ingest, or retraining
    occur here. Other requests can continue reading the last complete snapshot.
    """
    if settings.app_env != "dev":
        return "worker_managed"
    from spe.db.models import BoardSnapshot
    from spe.props.board_cache import WARM_MARKETS, entry_key, is_fresh

    snapshots = [db.get(BoardSnapshot, "props:" + entry_key(market, league, date))
                 for market in WARM_MARKETS]
    signature = scoring_signature()
    if all(s and is_fresh(s.payload) and s.payload.get("scoring_signature") == signature for s in snapshots):
        return "ready"
    key = (settings.database_url, date)
    with _lock:
        state = _states.get(key, {})
        if state.get("running"):
            return "running"
        if time.monotonic() - state.get("attempted", -1000) < 30:
            return "failed" if state.get("failed") else "running"
        _states[key] = {"running": True, "attempted": time.monotonic(), "failed": False}

    def rebuild():
        failed = False
        try:
            from spe.db.session import session_factory
            from spe.publishing.pipeline import publish_slate
            from spe.ingest.desk_evidence import refresh_local_evidence
            refresh_local_evidence()
            with session_factory() as worker:
                result = publish_slate(worker, date)
                worker.commit()
                logging.getLogger(__name__).info("Local research rebuilt for %s: %s rows", date, result["rows"])
        except Exception:
            failed = True
            logging.getLogger(__name__).exception("Local research rebuild failed for %s", date)
        finally:
            with _lock:
                _states[key].update(running=False, failed=failed)

    threading.Thread(target=rebuild, name=f"local-desk-{date}", daemon=True).start()
    return "running"
