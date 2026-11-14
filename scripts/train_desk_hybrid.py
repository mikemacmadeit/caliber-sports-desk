"""Train a research challenger from a local export or a read-only DB connection.

Example: python scripts/train_desk_hybrid.py --league nfl --input history.json
  --train-end 2024-08-01 --selection-end 2025-01-01
  --calibration-end 2026-02-15 --test-end 2026-11-14 --output data/research/hybrid

For a DB export use --database in place of --input. No paid APIs, downloads,
production model replacement, or table mutations are performed.
"""
import argparse
import json
from pathlib import Path

from spe.db.base import as_utc
from spe.models.player_td.desk_hybrid import VERSION, artifact_path, train_hybrid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", choices=("nfl", "fbs"), required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--database", action="store_true")
    parser.add_argument("--history-start", default="2016-07-01")
    for name in ("train-end", "selection-end", "calibration-end", "test-end"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == artifact_path(args.league).parent.resolve():
        parser.error("Choose a research output folder, not the live models directory")
    aliases = None
    if args.database:
        from spe.db.session import session_factory
        from spe.db.models import Team
        from spe.integrity.assignments import canonical_team_map
        from spe.models.player_td.desk_live import load_history
        with session_factory() as db:
            aliases = canonical_team_map(db.query(Team).all())
            records = load_history(db, args.league, as_utc(args.test_end), start=as_utc(args.history_start))
    else:
        body = json.loads(args.input.read_text())
        records = body["records"] if isinstance(body, dict) else body
        aliases = body.get("aliases") if isinstance(body, dict) else None
    model, report = train_hybrid(records, args.league, aliases=aliases,
        **{k: getattr(args, k) for k in ("train_end", "selection_end", "calibration_end", "test_end")})
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / f"{args.league}_{VERSION}.json"
    path.write_text(json.dumps(model, indent=2, allow_nan=False) + "\n")
    path.with_suffix(".report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"model": str(path), "research_only": True, "report": report}, indent=2))


if __name__ == "__main__":
    main()
