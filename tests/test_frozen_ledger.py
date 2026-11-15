"""Saturday's ledger is the card that was saved, not a later re-pick."""

from spe.publishing.ledger import ledger_day


def test_saturday_ledger_is_the_saved_card(db):
    card = ledger_day(db, "2026-11-14", "all")
    rows = card["rows"]
    assert len(rows) == 97
    charlotte = next(row for row in rows if row["sentence"] == "Charlotte +11.5")
    assert charlotte["result"] == "win"
    assert charlotte["detail"].startswith("East Carolina at Charlotte")
    henry = next(row for row in rows if row["sentence"] == "Tyrell Henry under 3.5 receptions")
    assert "-160" in henry["detail"]
    assert not any("Cole Tabb" in row["sentence"] for row in rows)
    spreads = [row for row in rows if row["market_id"] == "point_spread"]
    assert sum(row["result"] == "win" for row in spreads) == 22
    assert sum(row["result"] == "loss" for row in spreads) == 9
