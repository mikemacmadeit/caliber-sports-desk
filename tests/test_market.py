from spe.market.odds import (
    american_to_implied,
    ev_per_dollar,
    no_vig_anytime_td,
    no_vig_two_way,
)


def test_american_plus_and_minus():
    assert abs(american_to_implied(100) - 0.5) < 1e-9
    assert abs(american_to_implied(-110) - 110 / 210) < 1e-9


def test_blueprint_ev_example():
    # model p=.44, offered +180 → EV = .44*1.8 - .56 = .232
    ev = ev_per_dollar(0.44, 180)
    assert abs(ev - 0.232) < 1e-9


def test_yes_only_anytime_td_unidentified():
    result = no_vig_anytime_td(180, None)
    assert result.identified is False
    assert result.reason == "YES_ONLY_NOVIG_UNIDENTIFIED"
    assert result.p_yes is None


def test_two_way_devig():
    yes = american_to_implied(-120)
    no = american_to_implied(100)
    p_yes, p_no = no_vig_two_way(yes, no)
    assert abs(p_yes + p_no - 1) < 1e-9
    identified = no_vig_anytime_td(-120, 100)
    assert identified.identified is True
