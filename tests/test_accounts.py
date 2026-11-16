"""Accounts, the $19.99 flag, and hiding a call that has not kicked off."""

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.billing import apply_stripe_event
from app.gate import board_query_for, redact
from app.auth_routes import caller_address
from spe.accounts.auth import (
    AuthError,
    authenticate,
    begin_password_reset,
    finish_password_reset,
    is_subscribed,
    open_session,
    register_user,
    user_for_token,
)
from spe.db.models import AccountUser, AuthSession, PasswordReset, BillingEvent, BillingCursor

FUTURE = "2099-01-01T18:00:00+00:00"


def _db():
    engine = create_engine("sqlite://")
    AccountUser.__table__.create(engine)
    AuthSession.__table__.create(engine)
    PasswordReset.__table__.create(engine)
    BillingEvent.__table__.create(engine)
    BillingCursor.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_signup_login_and_a_wrong_password():
    db = _db()
    user = register_user(db, "Fan@Example.com", "password1")
    db.commit()
    assert user.email == "fan@example.com"
    assert authenticate(db, "fan@example.com", "password1").user_id == user.user_id
    try:
        authenticate(db, "fan@example.com", "nope")
        raise AssertionError("wrong password should fail")
    except AuthError as exc:
        assert exc.status == 401
    token = open_session(db, user)
    db.commit()
    assert user_for_token(db, token).user_id == user.user_id
    assert user_for_token(db, "not-a-session") is None


def test_duplicate_email_is_rejected():
    db = _db()
    register_user(db, "a@b.co", "password1")
    db.commit()
    try:
        register_user(db, "A@b.co", "password2")
        raise AssertionError("duplicate email should fail")
    except AuthError as exc:
        assert exc.status == 409


def test_only_an_active_unexpired_subscription_opens_the_desk():
    db = _db()
    user = register_user(db, "a@b.co", "password1")
    assert is_subscribed(user) is False
    user.subscription_status = "active"
    user.current_period_end = datetime(2099, 1, 1, tzinfo=UTC)
    assert is_subscribed(user) is True
    user.current_period_end = datetime(2020, 1, 1, tzinfo=UTC)
    assert is_subscribed(user) is False
    user.current_period_end = datetime(2099, 1, 1, tzinfo=UTC)
    user.subscription_status = "canceled"
    assert is_subscribed(user) is False
    user.is_admin = True
    assert is_subscribed(user) is True


def test_checkout_event_marks_the_account_active():
    db = _db()
    user = register_user(db, "a@b.co", "password1")
    apply_stripe_event(
        db,
        {
            "type": "checkout.session.completed",
            "id": "evt_checkout_1", "created": 1,
            "data": {
                "object": {
                    "client_reference_id": user.user_id,
                    "customer": "cus_1",
                    "mode": "subscription",
                    "status": "complete",
                    "subscription": {
                        "id": "sub_1",
                        "status": "active",
                        "customer": "cus_1",
                        "current_period_end": 4_102_444_800,
                    },
                }
            },
        },
    )
    assert user.subscription_status == "active"
    assert user.stripe_customer_id == "cus_1"
    assert is_subscribed(user) is True


def test_future_side_is_removed_and_a_final_stays():
    hidden = redact(
        "/v1/ats",
        {
            "rows": [
                {
                    "game_id": "g",
                    "kickoff_utc": FUTURE,
                    "game_status": "scheduled",
                    "home_team": "Home",
                    "away_team": "Away",
                    "pick_label": "Home to win",
                    "predicted_margin": 6.5,
                    "p_home_cover": 0.62,
                    "p_away_cover": 0.35,
                    "factors": {"ppg_edge": 6.0},
                }
            ]
        },
        subscribed=False,
    )
    assert hidden["rows"][0]["locked"] is True
    assert hidden["rows"][0]["pick_label"] is None
    assert hidden["rows"][0]["p_home_cover"] is None
    assert hidden["rows"][0]["factors"] is None
    assert hidden["rows"][0]["home_team"] == "Home"
    assert "Home to win" not in json.dumps(hidden)

    shown = redact(
        "/v1/ats",
        {
            "rows": [
                {
                    "game_id": "g",
                    "kickoff_utc": FUTURE,
                    "game_status": "final",
                    "home_team": "Home",
                    "away_team": "Away",
                    "pick_label": "Home to win",
                }
            ]
        },
        subscribed=False,
    )
    assert shown["rows"][0]["pick_label"] == "Home to win"


def test_a_future_prop_line_is_locked():
    out = redact(
        "/v1/props",
        {
            "rows": [
                {
                    "game_id": "g",
                    "kickoff_utc": FUTURE,
                    "player_name": "Secret Player",
                    "market_id": "anytime_td",
                    "book_price": -110,
                    "forecast": {"p_estimate": 0.44},
                }
            ]
        },
        subscribed=False,
    )
    assert out["locked"] is True
    assert out["rows"] == []
    assert "Secret Player" not in json.dumps(out)


def test_a_finished_prop_stays_on_the_board():
    out = redact(
        "/v1/props",
        {
            "rows": [
                {
                    "player_name": "Visible Player",
                    "kickoff_utc": "2020-01-01T18:00:00+00:00",
                    "forecast": {"p_estimate": 0.5},
                }
            ]
        },
        subscribed=False,
    )
    assert out["rows"][0]["player_name"] == "Visible Player"
    assert out.get("locked") is not True


def test_a_subscriber_still_sees_the_name():
    payload = {
        "rows": [
            {
                "player_name": "Secret Player",
                "kickoff_utc": FUTURE,
                "game_id": "g",
                "forecast": {"p_estimate": 0.44},
            }
        ]
    }
    assert redact("/v1/props", payload, subscribed=True)["rows"][0]["player_name"] == "Secret Player"


def test_a_future_prop_desk_call_is_locked():
    out = redact(
        "/v1/props/desk",
        {
            "calls": [
                {"player_name": "Secret Receiver", "kickoff_utc": FUTURE, "market_id": "receptions"},
                {"player_name": "Started Back", "kickoff_utc": "2020-01-01T18:00:00+00:00", "market_id": "rushing_yards"},
            ]
        },
        subscribed=False,
    )
    assert out["locked"] is True
    assert [call["player_name"] for call in out["calls"]] == ["Started Back"]
    assert "Secret Receiver" not in json.dumps(out)


def test_a_future_parlay_ticket_is_locked():
    out = redact(
        "/v1/parlays/card",
        {
            "tickets": [
                {
                    "title": "Best chance",
                    "joint_probability": 0.21,
                    "legs": [{"player_name": "Secret Player", "kickoff_utc": FUTURE}],
                }
            ],
            "eligible_legs": 4,
        },
        subscribed=False,
    )
    assert out["locked"] is True
    assert out["tickets"] == []
    assert out["eligible_legs"] == 0
    assert "Secret Player" not in json.dumps(out)


def test_the_home_desk_hides_a_future_player():
    out = redact(
        "/v1/desk",
        {
            "rows": [{"player": "Secret Player", "price": -110, "kickoff": FUTURE, "pick": "Over 33.5"}],
            "games": [{"matchup": "A at B"}],
            "total": 1,
            "qualified": 1,
            "books": ["DraftKings"],
            "blockers": [{"reason": "thin", "count": 1}],
            "market_counts": {"receptions": 1},
        },
        subscribed=False,
    )
    assert out["locked"] is True
    assert out["rows"] == []
    assert out["qualified"] == 0
    assert out["books"] == []
    assert out["games"][0]["matchup"] == "A at B"
    assert "Secret Player" not in json.dumps(out)


def test_free_board_cannot_sort_by_the_call():
    assert board_query_for("cover_prob", "favorites", subscribed=False) == ("kickoff", "all")
    assert board_query_for("cover_prob", "favorites", subscribed=True) == ("cover_prob", "favorites")


def test_open_recommendation_loses_the_player():
    out = redact(
        "/v1/recommendations/history",
        {"rows": [{"player": "Secret Player", "settlement": "open", "p": 0.4, "ev": 0.1}]},
        subscribed=False,
    )
    assert out["rows"][0]["player"] is None
    assert out["rows"][0]["locked"] is True


def test_a_reset_link_replaces_the_password_and_old_sessions():
    db = _db()
    user = register_user(db, "fan@example.com", "password1")
    token = open_session(db, user)
    db.commit()
    reset = begin_password_reset(db, "Fan@Example.com")
    assert reset is not None
    finish_password_reset(db, reset[1], "password2")
    db.commit()
    assert authenticate(db, "fan@example.com", "password2").user_id == user.user_id
    assert user_for_token(db, token) is None
    try:
        finish_password_reset(db, reset[1], "password3")
        raise AssertionError("a used link should fail")
    except AuthError as exc:
        assert exc.status == 400
    assert begin_password_reset(db, "missing@example.com") is None


def test_login_limits_follow_the_person_not_the_proxy():
    assert caller_address("203.0.113.8, 10.0.0.4", "10.0.0.4") == "203.0.113.8"
    assert caller_address("198.51.100.4, 203.0.113.8, 10.0.0.4", None) == "203.0.113.8"
