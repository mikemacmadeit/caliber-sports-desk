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
            if kind not in {"passing", "rushing", "receiving", "kickreturns", "puntreturns"}:
                continue
            labels = [str(s).upper() for s in group.get("labels") or []]
            for item in group.get("athletes") or []:
                athlete = item.get("athlete") or {}
                name = athlete.get("displayName")
                if not name:
                    continue
                values = dict(zip(labels, item.get("stats") or []))
                if kind == "passing":
                    # A QB with actual pass attempts participated even without a
                    # rushing/receiving row. Passing TDs never count as ATD.
                    try:
                        attempts = int(str(values.get("C/ATT", "")).split("/")[1])
                    except (IndexError, ValueError):
                        continue
                    if attempts <= 0:
                        continue
                row = people.setdefault(
                    str(athlete.get("id") or name),
                    {
                        "name": name,
                        "espn_id": str(athlete.get("id") or ""),
                        "markets": {},
                        "td": 0,
                        "td_complete": True,
                    },
                )
                if kind == "passing":
                    continue

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


def box_actual(markets: dict | None, market: str, *, final: bool) -> float | None:
    """Use the listed number. A finished box that omits the player or the stat recorded zero.

    A box that never loaded stays unknown. A listed player with no touchdown column
    stays unknown rather than a made-up zero.
    """
    if markets is not None and market in markets:
        return float(markets[market])
    if not final:
        return None
    if markets is None:
        return 0.0
    if market == "anytime_td":
        return None
    return 0.0


def _matched_player(person: dict, catalog: list[tuple[str, str]]) -> str | None:
    espn_id = str(person.get("espn_id") or "")
    if espn_id:
        for player_id, _name in catalog:
            if str(player_id).split(":")[-1] == espn_id:
                return str(player_id)
    match = match_name(str(person.get("name") or ""), catalog)
    if match and match.review_status == "auto" and match.canonical_id:
        return str(match.canonical_id)
    return None


def _pending_stat(db, key: str):
    stat = db.get(PublishedStat, key)
    if stat is not None:
        return stat
    for obj in db.new:
        if isinstance(obj, PublishedStat) and obj.key == key:
            return obj
    return None


def _put_stat(db, game_id: str, player_id: str, market: str, actual: float, source: str, *, only_missing: bool = False) -> bool:
    key = f"{game_id}|{player_id}|{market}"
    stat = _pending_stat(db, key)
    if stat is not None and only_missing:
        return False
    if stat is None:
        stat = PublishedStat(key=key, game_id=game_id, player_id=player_id, market_id=market)
        db.add(stat)
    stat.actual = float(actual)
    stat.observed_at = datetime.now(UTC)
    stat.source = source
    return True


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
        picks = [r for r in pending if r.game_id == gid]
        catalog = list({(r.snapshot["player_id"], r.snapshot["player_name"]) for r in picks})
        people = parse(payload)
        if not people:
            failed += 1
            continue
        found: dict[str, dict] = {}
        for row in people:
            player_id = _matched_player(row, catalog)
            if not player_id:
                continue
            found[player_id] = row["markets"]
            for market, value in row["markets"].items():
                if _put_stat(db, gid, player_id, market, value, f"espn.summary:{event_id}"):
                    written += 1
        for pick in picks:
            player_id = str(pick.snapshot.get("player_id") or "")
            if not player_id:
                continue
            actual = box_actual(found.get(player_id), pick.market_id, final=True)
            if actual is None:
                continue
            if _put_stat(db, gid, player_id, pick.market_id, actual, f"espn.summary.omitted:{event_id}", only_missing=True):
                written += 1
    db.flush()
    return {"status": "degraded" if failed else "ok", "written": written, "failed_games": failed}
