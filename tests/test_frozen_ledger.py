"""Saturday's ledger is the card that was saved, not a later re-pick."""

from spe.publishing.ledger import ledger_calendar, ledger_day, ledger_year


def test_saturday_ledger_is_the_saved_card(db):
    card = ledger_day(db, "2026-11-14", "all")
    rows = card["rows"]
    assert card["record_status"] == "saved_pregame"
    assert "note" not in card
    assert len(rows) == 97
    charlotte = next(row for row in rows if row["sentence"] == "Charlotte +11.5")
    assert charlotte["result"] == "win"
    assert charlotte["units"] == 0.9090909090909092
    assert charlotte["detail"].startswith("East Carolina at Charlotte")
    henry = next(row for row in rows if row["sentence"] == "Tyrell Henry under 3.5 receptions")
    assert "-160" in henry["detail"]
    assert henry["units"] == 0.625
    assert not any("Cole Tabb" in row["sentence"] for row in rows)
    spreads = [row for row in rows if row["market_id"] == "point_spread"]
    assert sum(row["result"] == "win" for row in spreads) == 22
    assert sum(row["result"] == "loss" for row in spreads) == 9
    assert card["summary"]["priced_bets"] > 0
    november = ledger_calendar(db, "2026-11", "all")
    assert "2026-11-14" in november["units"]
    year = ledger_year(db, 2026, "all")
    assert year["days"] >= 1
    assert year["priced"] >= card["summary"]["priced_bets"]


def test_saved_card_fills_a_price_and_a_missing_pick(db):
    from spe.publishing.ledger import _merge_saved

    live = [{
        "pick_id": "live-td",
        "market_id": "anytime_td",
        "sentence": "Amon-Ra St. Brown anytime touchdown",
        "detail": "",
        "result": "win",
        "units": None,
        "kickoff_utc": "2026-11-15T18:00:00+00:00",
    }]
    saved = [
        {
            "pick_id": "saved-td",
            "market_id": "anytime_td",
            "sentence": "Amon-Ra St. Brown anytime touchdown",
            "detail": "draftkings · -135",
            "result": "win",
            "units": 0.7407407407407407,
            "kickoff_utc": "2026-11-15T18:00:00+00:00",
        },
        {
            "pick_id": "saved-spread",
            "market_id": "point_spread",
            "sentence": "Detroit Lions -3",
            "detail": "Detroit Lions at somewhere · -110",
            "result": "win",
            "units": 0.9090909090909091,
            "kickoff_utc": "2026-11-15T18:00:00+00:00",
        },
    ]
    rows = _merge_saved(live, saved)
    assert len(rows) == 2
    touchdown = next(row for row in rows if row["market_id"] == "anytime_td")
    assert touchdown["pick_id"] == "live-td"
    assert touchdown["units"] == 0.7407407407407407
    assert "-135" in touchdown["detail"]
    assert any(row["sentence"] == "Detroit Lions -3" for row in rows)
