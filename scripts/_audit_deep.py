"""Deeper probes for Caliber E2E audit."""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))
sys.path.insert(0, str(ROOT / "services" / "api"))

from sqlalchemy import text
from spe.db import models as m
from spe.db.session import session_factory
from spe.ingest.model_forecasts import publish_model_forecasts


def main() -> None:
    s = session_factory()
    s.connection().exec_driver_sql("PRAGMA busy_timeout=300000")
    out: dict = {}

    # Roster week current
    out["roster_week_2026"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select team_id, max(week), count(*)
                from roster_weeks
                where season_year=2026 and team_id in ('nfl:CAR','nfl:DEN')
                group by team_id
                """
            )
        ).fetchall()
    ]
    out["retired_on_2026_roster_weeks"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select p.name, rw.season_year, rw.week, rw.team_id
                from roster_weeks rw
                join players p on p.player_id=rw.player_id
                where p.name in ('Cam Newton','Luke Kuechly','Greg Olsen','Ted Ginn','Thomas Davis')
                  and rw.season_year=2026
                """
            )
        ).fetchall()
    ]
    out["active_car_den_week_max"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select p.name, p.position_group, rw.team_id, rw.week, rw.status
                from roster_weeks rw
                join players p on p.player_id=rw.player_id
                where rw.season_year=2026
                  and rw.team_id in ('nfl:CAR','nfl:DEN')
                  and rw.week = (
                    select max(week) from roster_weeks rw2
                    where rw2.season_year=2026 and rw2.team_id=rw.team_id
                  )
                order by rw.team_id, p.position_group, p.name
                limit 60
                """
            )
        ).fetchall()
    ]

    # Features
    out["features_for_den_car"] = s.execute(
        text("select count(*) from features where game_id='nfl:2026_09_DEN_CAR'")
    ).scalar()
    out["feature_prefixes"] = [
        list(r)
        for r in s.execute(
            text(
                "select substr(game_id,1,16), count(*) from features group by 1 order by 2 desc limit 20"
            )
        ).fetchall()
    ]

    # How forecasts get means — inspect publish path / artifact
    art = Path("data/parquet/models")
    out["model_artifacts"] = [p.name for p in art.glob("*")] if art.exists() else []

    # Identical FBS TD probs
    rows = s.execute(
        text(
            """
            select p.name, o.player_id, f.p, f.components
            from forecasts f
            join outcomes o on o.outcome_id=f.outcome_id
            join players p on p.player_id=o.player_id
            where o.game_id='espn:fbs:401856855' and o.market_id='anytime_td'
              and f.as_of=(select max(f2.as_of) from forecasts f2 where f2.outcome_id=f.outcome_id)
            order by f.p desc
            """
        )
    ).mappings().all()
    p_counts = Counter(round(r["p"], 8) for r in rows)
    out["fbs_td_unique_p_values"] = len(p_counts)
    out["fbs_td_top_duplicate_ps"] = p_counts.most_common(5)
    def _comp(raw):
        if raw is None:
            return {}
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return raw if isinstance(raw, dict) else {}

    out["fbs_td_players"] = [
        {
            "name": r["name"],
            "p": r["p"],
            "mean": _comp(r["components"]).get("mean"),
            "pos": _comp(r["components"]).get("position_group"),
        }
        for r in rows[:20]
    ]

    # NFL TD diversity
    rows_n = s.execute(
        text(
            """
            select p.name, o.player_id, f.p, f.components
            from forecasts f
            join outcomes o on o.outcome_id=f.outcome_id
            join players p on p.player_id=o.player_id
            where o.game_id='nfl:2026_09_DEN_CAR' and o.market_id='anytime_td'
              and f.as_of=(select max(f2.as_of) from forecasts f2 where f2.outcome_id=f.outcome_id)
            order by f.p desc
            """
        )
    ).mappings().all()
    out["nfl_td_players"] = [
        {
            "name": r["name"],
            "p": r["p"],
            "mean": _comp(r["components"]).get("mean"),
            "pos": _comp(r["components"]).get("position_group"),
        }
        for r in rows_n
    ]
    out["nfl_td_unique_p"] = len({round(r["p"], 8) for r in rows_n})

    # Unlinked rushing outcomes
    out["null_player_outcomes_upcoming"] = s.execute(
        text(
            """
            select count(*) from outcomes o
            join games g on g.game_id=o.game_id
            where o.player_id is null and g.kickoff_utc >= datetime('now')
            """
        )
    ).scalar()
    out["null_player_sample"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select o.outcome_id, o.game_id, o.market_id, o.side, o.line, o.description
                from outcomes o
                join games g on g.game_id=o.game_id
                where o.player_id is null and g.kickoff_utc >= datetime('now')
                limit 15
                """
            )
        ).fetchall()
    ]

    # Injury team check
    out["injury_team_match"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select p.name, i.designation,
                  (select team_id from roster_weeks rw
                   where rw.player_id=i.player_id and rw.season_year=2026
                   order by rw.week desc limit 1) team2026
                from injury_events i
                join players p on p.player_id=i.player_id
                where i.game_id='nfl:2026_09_DEN_CAR'
                """
            )
        ).fetchall()
    ]

    # Sim vs model with full IDs
    run = s.query(m.SimulationRun).filter(m.SimulationRun.game_id == "nfl:2026_09_DEN_CAR").order_by(m.SimulationRun.as_of.desc()).first()
    sim_td = (run.summary or {}).get("player_td_prob") or {} if run else {}
    model_td = {r["player_id"]: r["p"] for r in rows_n}
    # also map by name from sim ids
    diffs = []
    for pid, mp in model_td.items():
        sp = sim_td.get(pid)
        if sp is None:
            diffs.append({"player_id": pid, "model_p": mp, "sim_p": None, "note": "missing_from_sim"})
        else:
            diffs.append({"player_id": pid, "model_p": mp, "sim_p": sp, "abs_diff": abs(sp - mp)})
    # retired players in sim top
    top_sim = sorted(sim_td.items(), key=lambda x: -x[1])[:15]
    top_named = []
    for pid, p in top_sim:
        pl = s.get(m.Player, pid)
        top_named.append({"player_id": pid, "name": pl.name if pl else None, "sim_p": p})
    out["sim_vs_model"] = {
        "model_players": len(model_td),
        "sim_players": len(sim_td),
        "model_in_sim": sum(1 for d in diffs if d.get("sim_p") is not None),
        "missing_from_sim": [d for d in diffs if d.get("sim_p") is None][:20],
        "compared_diffs": sorted([d for d in diffs if "abs_diff" in d], key=lambda x: -x["abs_diff"])[:15],
        "top_sim_scorers": top_named,
    }

    # Current-season usage evidence for McMillan / Hubbard
    out["player_analytics_2026"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select p.name, pa.season_year, pa.grain, pa.split, pa.metrics
                from player_analytics pa
                join players p on p.player_id=pa.player_id
                where p.name in ('Chuba Hubbard','Tetairoa McMillan','Bo Nix')
                  and pa.league_id='nfl'
                order by p.name, pa.season_year desc
                limit 20
                """
            )
        ).fetchall()
    ]

    # What does the model actually use for means?
    from spe.models import train as train_mod
    import inspect

    src = Path(train_mod.__file__).read_text(encoding="utf-8")[:500]
    out["train_module_head"] = src

    # Read publish_model_forecasts / predict path briefly via DB feature absence
    # Shadow freeze fields
    out["shadow_pred_cols"] = [
        list(r) for r in s.execute(text("pragma table_info(shadow_predictions)")).fetchall()
    ]
    cols = [c[1] for c in s.execute(text("pragma table_info(shadow_predictions)")).fetchall()]
    out["shadow_cols"] = cols
    row = s.execute(
        text(
            """
            select shadow_id, game_id, outcome_id, forecast_probability, recommendation_status,
                   research_only, play_enabled, created_at, abstain_reasons, forecast_as_of
            from shadow_predictions
            where game_id='nfl:2026_09_DEN_CAR'
            limit 5
            """
        )
    ).mappings().all()
    out["shadow_sample"] = [dict(r) for r in row]
    out["shadow_dup_outcomes"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select outcome_id, count(*) c from shadow_predictions
                group by outcome_id having c>1
                order by c desc limit 10
                """
            )
        ).fetchall()
    ]
    out["shadow_settlement_count"] = s.query(m.ShadowSettlement).count()
    out["settlement_count"] = s.query(m.Settlement).count()

    # Track record error
    try:
        urllib.request.urlopen("http://localhost:8000/v1/shadow/track-record", timeout=30)
        out["track_record_ok"] = True
    except Exception as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        out["track_record_error"] = body[:3000]

    # ESPN identity split
    out["den_car_ids"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select game_id, home_team_id, away_team_id, kickoff_utc,
                  (select count(*) from forecasts f join outcomes o on o.outcome_id=f.outcome_id where o.game_id=g.game_id) fc,
                  (select count(*) from odds_ticks ot join outcomes o on o.outcome_id=ot.outcome_id where o.game_id=g.game_id) ticks
                from games g
                where (home_team_id like '%CAR%' and away_team_id like '%DEN%')
                   or game_id like '%401873041%'
                   or (game_id like '%DEN_CAR%')
                """
            )
        ).fetchall()
    ]

    # Seed games on slate API
    slate = json.loads(urllib.request.urlopen("http://localhost:8000/v1/slate?league=nfl").read())
    out["slate_game_ids"] = [g.get("game_id") or g.get("id") for g in slate.get("games", [])]
    out["seed_on_slate"] = [g for g in out["slate_game_ids"] if g and (g.startswith("nfl-") and ":" not in g)]

    # Okereke team
    out["okereke"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select p.name, rw.team_id, rw.week, rw.status
                from players p
                join roster_weeks rw on rw.player_id=p.player_id
                where p.name like '%Okereke%' and rw.season_year=2026
                order by rw.week desc limit 5
                """
            )
        ).fetchall()
    ]

    # Job cadence — are scheduled jobs continuous or manual?
    out["job_run_span"] = [
        list(r)
        for r in s.execute(
            text(
                """
                select job_name, count(*), min(started_at), max(started_at),
                       sum(case when status!='ok' then 1 else 0 end) failures
                from job_runs
                group by job_name
                order by max(started_at) desc
                """
            )
        ).fetchall()
    ]

    # Multiple lines monotonicity deeper — check all over lines for McMillan-like
    mono = s.execute(
        text(
            """
            select p.name, o.market_id, o.line, f.p
            from forecasts f
            join outcomes o on o.outcome_id=f.outcome_id
            join players p on p.player_id=o.player_id
            where o.game_id='nfl:2026_09_DEN_CAR'
              and o.side='over'
              and o.line is not null
              and f.as_of=(select max(f2.as_of) from forecasts f2 where f2.outcome_id=f.outcome_id)
            order by p.name, o.market_id, o.line
            """
        )
    ).mappings().all()
    out["over_lines"] = [dict(r) for r in mono]

    # Mean vs sportsbook realism: model mean 27 vs line 62.5 for McMillan
    out["mean_vs_line"] = [
        {
            "name": r["name"],
            "market": r["market_id"],
            "line": r["line"],
            "p_over": r["p"],
            "mean": None,
        }
        for r in mono[:30]
    ]
    # attach means
    for item in out["mean_vs_line"]:
        row = s.execute(
            text(
                """
                select f.components from forecasts f
                join outcomes o on o.outcome_id=f.outcome_id
                join players p on p.player_id=o.player_id
                where o.game_id='nfl:2026_09_DEN_CAR' and p.name=:n and o.market_id=:m and o.side='over' and o.line=:l
                order by f.as_of desc limit 1
                """
            ),
            {"n": item["name"], "m": item["market"], "l": item["line"]},
        ).mappings().first()
        if row:
            item["mean"] = _comp(row["components"]).get("mean")

    Path("data/audit_deep.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("WROTE data/audit_deep.json")
    for k in [
        "roster_week_2026",
        "retired_on_2026_roster_weeks",
        "features_for_den_car",
        "fbs_td_unique_p_values",
        "fbs_td_top_duplicate_ps",
        "nfl_td_unique_p",
        "null_player_outcomes_upcoming",
        "sim_vs_model",
        "shadow_settlement_count",
        "seed_on_slate",
        "job_run_span",
        "den_car_ids",
        "okereke",
    ]:
        print(k, json.dumps(out.get(k), default=str)[:1500])


if __name__ == "__main__":
    main()
