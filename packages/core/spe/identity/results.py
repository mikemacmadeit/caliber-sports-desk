"""Match official results conservatively; ambiguous identities remain ungraded."""
from collections import defaultdict

from spe.db.models import ProviderIdMap
from spe.identity.match import normalize_name


def espn_player_ids(db, player_ids):
    ids = defaultdict(set)
    for pid in player_ids:
        # A numeric suffix in another provider's identifier is not an ESPN id.
        if str(pid).startswith("espn:"):
            ids[str(pid)].add(str(pid).split(":")[-1])
    if player_ids:
        for row in db.query(ProviderIdMap).filter(
            ProviderIdMap.entity_type == "player",
            ProviderIdMap.canonical_id.in_(player_ids),
            ProviderIdMap.provider.like("espn%"),
            ProviderIdMap.review_status.in_(("auto", "approved", "manual")),
        ):
            ids[row.canonical_id].add(row.external_id.split(":")[-1])
    return ids


def match_result(people, player_id, name, ids, team=None):
    expected = ids.get(str(player_id), set())
    if expected:
        matches = [p for p in people if p.get("espn_id") in expected]
        return matches[0] if len(matches) == 1 else None
    matches = [p for p in people if normalize_name(p.get("name", "")) == normalize_name(name or "")]
    # Team only breaks a tie. BUF and BUFF are the same school, so an exact
    # abbreviation check was leaving the only player in the box ungraded.
    if team and len(matches) > 1:
        same = [p for p in matches if not p.get("team") or p["team"].upper() == str(team).upper()]
        if same:
            matches = same
    return matches[0] if len(matches) == 1 else None
