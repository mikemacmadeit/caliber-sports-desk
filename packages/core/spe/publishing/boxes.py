"""Market-specific results for published picks; incomplete rows stay pending."""

from datetime import UTC, datetime
from spe.db.models import PublishedPick, PublishedResult, PublishedStat, Game
from spe.identity.match import match_name
from spe.ingest.espn_box import _fetch_summary, _espn_event_id


def parse(payload):
    people = {}
    for team in (payload.get("boxscore") or {}).get("players") or []:
        for group in team.get("statistics") or []:
            kind = str(group.get("name", "")).lower()
            if kind not in {"rushing", "receiving", "kickreturns", "puntreturns"}:
                continue
            labels = [str(s).upper() for s in group.get("labels") or []]
            for item in group.get("athletes") or []:
                athlete = item.get("athlete") or {}
                name = athlete.get("displayName")
                if not name:
                    continue
                values = dict(zip(labels, item.get("stats") or []))
                row = people.setdefault(
                    str(athlete.get("id") or name),
                    {"name": name, "markets": {}, "td": 0, "td_complete": True},
                )

                def number(label):
                    try:
                        value = float(values[label])
                        import math

                        return value if math.isfinite(value) else None
                    except (ValueError, TypeError, KeyError):
                        return None

                td = number("TD")
                row["td_complete"] &= td is not None
                row["td"] += td or 0
                if kind == "rushing" and number("YDS") is not None:
                    row["markets"]["rushing_yards"] = number("YDS")
                if kind == "receiving":
                    for label, market in (("YDS", "receiving_yards"), ("REC", "receptions")):
                        if number(label) is not None:
                            row["markets"][market] = number(label)
    for row in people.values():
        if row["td_complete"]:
            row["markets"]["anytime_td"] = row["td"]
    return list(people.values())


def ingest(db, fetch=_fetch_summary):
    pending = (
        db.query(PublishedPick)
        .filter(~PublishedPick.pick_id.in_(db.query(PublishedResult.pick_id)))
        .all()
    )
    games = {r.game_id for r in pending}
    written = failed = 0
    for gid in games:
        game = db.get(Game, gid)
        if not game or game.status != "final":
            continue
        event_id = _espn_event_id(db, game)
        payload = fetch(game.league_id, event_id) if event_id else None
        if not payload:
            failed += 1
            continue
        catalog = list(
            {
                (r.snapshot["player_id"], r.snapshot["player_name"])
                for r in pending
                if r.game_id == gid
            }
        )
        for row in parse(payload):
            match = match_name(row["name"], catalog)
            if not match or match.review_status != "auto":
                continue
            for market, value in row["markets"].items():
                key = f"{gid}|{match.canonical_id}|{market}"
                stat = db.get(PublishedStat, key)
                if stat is None:
                    stat = PublishedStat(
                        key=key, game_id=gid, player_id=match.canonical_id, market_id=market
                    )
                    db.add(stat)
                stat.actual, stat.observed_at, stat.source = (
                    value,
                    datetime.now(UTC),
                    f"espn.summary:{event_id}",
                )
                written += 1
    db.flush()
    return {"status": "degraded" if failed else "ok", "written": written, "failed_games": failed}
