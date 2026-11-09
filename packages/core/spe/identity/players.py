"""Canonical NFL player identity. GSIS is canonical; ESPN ids are aliases only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from spe.config import settings
from spe.db.models import Outcome, Player, ProviderIdMap
from spe.identity.games import remap_provider
from spe.ingest.canonical import nfl_player_id, position_group
from spe.ingest.values import as_str


def resolve_nfl_player_id(session: Session, external_id: str, *, provider: str = "espn") -> str | None:
    """Map a provider athlete id to ``nfl:{gsis}``. Never invents a GSIS id."""
    ext = str(external_id or "").strip()
    if not ext:
        return None
    if ext.startswith("00-"):
        pid = nfl_player_id(ext)
        return pid if session.get(Player, pid) is not None else None
    if ext.startswith("nfl:00-"):
        return ext if session.get(Player, ext) is not None else None
    row = (
        session.query(ProviderIdMap)
        .filter(
            ProviderIdMap.provider == provider,
            ProviderIdMap.entity_type == "player",
            ProviderIdMap.external_id == ext,
        )
        .one_or_none()
    )
    if row and row.canonical_id and session.get(Player, row.canonical_id) is not None:
        return row.canonical_id
    # Accidental espn-style primary keys left behind by the retired slate writer.
    fake = f"nfl:{ext}"
    if session.get(Player, fake) is not None and not ext.startswith("00-"):
        mapped = (
            session.query(ProviderIdMap)
            .filter(ProviderIdMap.provider == "espn", ProviderIdMap.entity_type == "player", ProviderIdMap.external_id == ext)
            .one_or_none()
        )
        if mapped:
            return mapped.canonical_id
    return None


def load_espn_gsis_map(path: Path | None = None) -> dict[str, str]:
    """espn_id -> gsis_id from the cached nflverse players file. Empty when the file is absent."""
    candidates = []
    if path is not None:
        candidates.append(path)
    root = Path(settings.parquet_dir) if hasattr(settings, "parquet_dir") else Path("data")
    candidates.extend(
        [
            root / "cache" / "nflverse" / "players.parquet",
            root / "cache" / "players.parquet",
            Path("data/cache/nflverse/players.parquet"),
            Path("data/cache/players.parquet"),
        ]
    )
    for candidate in candidates:
        if candidate is None or not Path(candidate).exists():
            continue
        df = pd.read_parquet(candidate, columns=["gsis_id", "espn_id"])
        out: dict[str, str] = {}
        for _, raw in df.iterrows():
            gsis = as_str(raw.get("gsis_id"))
            espn = as_str(raw.get("espn_id"))
            if not gsis or not espn:
                continue
            out[str(int(float(espn))) if str(espn).replace(".", "", 1).isdigit() else str(espn)] = gsis
        return out
    return {}


def sync_espn_player_maps(session: Session, *, path: Path | None = None) -> dict[str, Any]:
    """Write espn → nfl:{gsis} provider maps. Does not create players that are not already stored."""
    mapping = load_espn_gsis_map(path)
    written = skipped_missing = 0
    for espn_id, gsis in mapping.items():
        pid = nfl_player_id(gsis)
        if session.get(Player, pid) is None:
            skipped_missing += 1
            continue
        remap_provider(
            session,
            "espn",
            "player",
            espn_id,
            pid,
            notes="phase1_espn_gsis",
        )
        written += 1
        if written % 2000 == 0:
            session.flush()
    session.flush()
    return {"mapped": written, "skipped_missing_player": skipped_missing, "source_rows": len(mapping)}


def remint_espn_style_outcomes(session: Session) -> dict[str, Any]:
    """Retarget outcomes that still point at ``nfl:{espn_numeric}`` onto GSIS when mapped."""
    rows = (
        session.query(Outcome)
        .filter(Outcome.player_id.is_not(None), Outcome.player_id.like("nfl:%"))
        .all()
    )
    remapped = unmatched = unchanged = 0
    for outcome in rows:
        pid = outcome.player_id or ""
        if pid.startswith("nfl:00-"):
            unchanged += 1
            continue
        ext = pid.split(":", 1)[-1]
        if not ext.isdigit():
            unchanged += 1
            continue
        canonical = resolve_nfl_player_id(session, ext, provider="espn")
        if not canonical:
            unmatched += 1
            continue
        outcome.player_id = canonical
        remapped += 1
    session.flush()
    return {"remapped": remapped, "unmatched": unmatched, "unchanged": unchanged}


def ensure_gsis_player_from_row(session: Session, raw: dict[str, Any]) -> str | None:
    """Upsert a GSIS player and optional ESPN alias from an nflverse players row."""
    gsis = as_str(raw.get("gsis_id")) or as_str(raw.get("player_id"))
    name = as_str(raw.get("display_name")) or as_str(raw.get("player_name"))
    if not gsis or not name:
        return None
    pid = nfl_player_id(gsis)
    pos = as_str(raw.get("position")) or "UNK"
    row = session.get(Player, pid)
    if row is None:
        session.add(
            Player(
                player_id=pid,
                name=name[:128],
                birth_date=as_str(raw.get("birth_date")),
                position_group=as_str(raw.get("position_group")) or position_group(pos),
                position=pos[:16],
                aliases=[],
            )
        )
        session.flush()
    remap_provider(session, "nflverse", "player", gsis, pid)
    espn = as_str(raw.get("espn_id"))
    if espn:
        espn_key = str(int(float(espn))) if str(espn).replace(".", "", 1).isdigit() else espn
        remap_provider(session, "espn", "player", espn_key, pid, notes="phase1_espn_gsis")
    return pid
