import json
from pathlib import Path

r = json.loads(Path("data/phase1_repair_report.json").read_text(encoding="utf-8"))
print("ok", r.get("ok"), "play", r.get("play_enabled"), "validated", r.get("validated_play"))
print("canonical", r.get("canonical_games"))
print("player_maps", r.get("player_maps"))
print("outcome_remap", r.get("outcome_player_remap"))
print("rosters", r.get("rosters"))
uf = r.get("upcoming_features") or {}
print("features_status", uf.get("status"), "team_rows", uf.get("team_rows"), "player_rows", uf.get("player_rows"), "games", len(uf.get("game_ids") or []))
print("leagues", uf.get("leagues"))
fa = r.get("forecast_audit") or {}
print("identity", fa.get("identity"))
print("stale", fa.get("stale_model"))
mf = fa.get("missing_features") or {}
print("missing_features_flagged", mf.get("flagged"), "games", len(mf.get("games_missing_features") or []))
print("alias", fa.get("alias_game"))
for row in r.get("verification") or []:
    print(
        "verify",
        row["game_id"],
        "ok",
        row["ok"],
        "features",
        row["feature_rows"],
        "hist",
        row.get("player_features_with_history"),
        "eligible",
        row["roster"]["eligible_count"],
    )

after = Path("data/phase1_audit_after.json")
if after.exists():
    a = json.loads(after.read_text(encoding="utf-8"))
    print("POST_AUDIT dups", a.get("duplicate_groups"), "rosters", a.get("rosters"), "features", a.get("features_atd"))
