"""Customer and production failure modes, with no live email/payment/network calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth_routes, billing
from spe.accounts.auth import is_subscribed, register_user
from spe.accounts.limits import consume
from spe.db.models import BillingEvent, JobRun


def request(headers=()):
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers,
                    "client": ("127.0.0.1", 1234)})


def test_reset_requests_consume_limit_for_unknown_and_known_accounts(db, monkeypatch):
    monkeypatch.setattr(auth_routes, "mail_configured", lambda: True)
    sent = []
    monkeypatch.setattr(auth_routes, "send_password_reset", lambda *args: sent.append(args))
    register_user(db, "known@example.com", "long-password")
    db.commit()
    for email in ("known@example.com", "unknown@example.com"):
        for _ in range(8):
            assert auth_routes.forgot(auth_routes.ResetRequest(email=email), request(), db) == {"ok": True}
            db.commit()
        with pytest.raises(HTTPException) as exc:
            auth_routes.forgot(auth_routes.ResetRequest(email=email), request(), db)
        assert exc.value.status_code == 429
    assert len(sent) == 8


def test_attempts_survive_new_sessions_and_concurrent_requests(db):
    engine = db.get_bind()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: consume(engine, [("same-account", 8)]), range(20)))
    assert sum(results) == 8
    assert consume(engine, [("different-account", 8)])
    assert consume(engine, [("same-account", 8)], now=datetime.now(UTC) + timedelta(minutes=11))


def test_unsigned_forwarding_headers_cannot_change_auth_identity():
    assert auth_routes._caller(request([(b"x-forwarded-for", b"8.8.8.8")])) == "127.0.0.1"


def event(user, key, created, status, *, sub="sub_1"):
    return {"id": key, "created": created,
            "type": "customer.subscription.deleted" if status == "canceled" else "customer.subscription.updated",
            "data": {"object": {"id": sub, "customer": "cus_test", "status": status,
                "current_period_end": 4102444800, "metadata": {"user_id": user.user_id}}}}


def test_duplicate_and_delayed_stripe_events_never_restore_canceled_access(db):
    user = register_user(db, "bill@example.com", "long-password")
    billing.apply_stripe_event(db, event(user, "evt_1", 10, "active")); db.commit()
    assert is_subscribed(user)
    billing.apply_stripe_event(db, event(user, "evt_2", 20, "canceled")); db.commit()
    billing.apply_stripe_event(db, event(user, "evt_1", 10, "active")); db.commit()
    billing.apply_stripe_event(db, event(user, "evt_3", 15, "active")); db.commit()
    billing.apply_stripe_event(db, event(user, "evt_4", 20, "active")); db.commit()
    assert not is_subscribed(user)
    assert db.query(BillingEvent).count() == 4


def test_old_subscription_cancellation_does_not_cancel_replacement(db):
    user = register_user(db, "bill@example.com", "long-password")
    billing.apply_stripe_event(db, event(user, "evt_1", 10, "active", sub="sub_new")); db.commit()
    billing.apply_stripe_event(db, event(user, "evt_2", 20, "canceled", sub="sub_old")); db.commit()
    assert is_subscribed(user) and user.subscription_id == "sub_new"


def test_unresolved_billing_event_rolls_back_receipt_and_can_retry(db):
    missing = SimpleNamespace(user_id="missing")
    with pytest.raises(billing.BillingError):
        billing.apply_stripe_event(db, event(missing, "evt_retry", 1, "active"))
    db.rollback()
    assert db.get(BillingEvent, "evt_retry") is None


def test_active_subscription_without_period_is_not_an_entitlement(db):
    user = register_user(db, "bill@example.com", "long-password")
    user.subscription_status = "active"
    assert not is_subscribed(user)


def test_checkout_retries_share_key_and_active_users_cannot_buy_twice(db, monkeypatch):
    user = register_user(db, "bill@example.com", "long-password")
    user.email_verified_at = datetime.now(UTC)
    calls = []
    stripe = SimpleNamespace(checkout=SimpleNamespace(Session=SimpleNamespace(
        create=lambda **kw: (calls.append(kw) or {"url": "https://checkout.stripe.com/test"}))))
    monkeypatch.setattr(billing, "_stripe", lambda: stripe)
    billing.create_checkout(user); billing.create_checkout(user)
    assert calls[0]["idempotency_key"] == calls[1]["idempotency_key"]
    user.subscription_status = "active"
    user.current_period_end = datetime(2099, 1, 1, tzinfo=UTC)
    with pytest.raises(billing.BillingError) as exc:
        billing.create_checkout(user)
    assert exc.value.status == 409 and len(calls) == 2


def test_optional_injury_feed_does_not_discard_free_sources(db, monkeypatch):
    from spe.publishing.pipeline import inputs
    monkeypatch.setattr("spe.ingest.nfl_availability.refresh_nfl_availability", lambda *a: {"status": "ok"})
    monkeypatch.setattr("spe.ingest.espn_availability.ingest_espn_availability", lambda *a: {"status": "ok"})
    monkeypatch.setattr("spe.ingest.api_sports_injuries.ingest_api_sports_injuries", lambda *a, **k: {"status": "needs_key"})
    result = inputs(db)
    assert result["status"] == "ok" and result["warnings"]
    monkeypatch.setattr("spe.ingest.nfl_availability.refresh_nfl_availability", lambda *a: {"status": "unavailable"})
    assert inputs(db)["status"] == "degraded"


def test_unpriced_scores_never_generate_profit_or_enter_roi():
    from spe.publishing.ledger import _ledger_rows, _ledger_summary
    rows = _ledger_rows([], [], [{"game_id": "g", "result": "win", "market_id": "anytime_td"}])
    assert rows[0]["units"] is None
    result = _ledger_summary(rows)
    assert result["win"] == 1 and result["roi"] is None


def test_card_keeps_comparisons_without_internal_features():
    from spe.models.player_td.presentation import card_payload
    original = {"rows": [{"player_id": "p", "desk_analysis": {"features": {"huge": [1]*100}},
        "decision": {"most_likely": {"player_id": "p"}, "alternatives": [{"player_id": "a", "name": "Alt", "probability": .3}]}}]}
    card = card_payload(original)
    assert "features" not in card["rows"][0]["desk_analysis"]
    assert card["rows"][0]["decision"]["alternatives"][0]["name"] == "Alt"
    assert "features" in original["rows"][0]["desk_analysis"]


def test_verification_is_one_use_and_deletion_revokes_sessions(db):
    from spe.accounts.auth import begin_email_verification, finish_email_verification, remove_account, open_session, user_for_token, AuthError
    user = register_user(db, "remove@example.com", "long-password")
    token = open_session(db, user)
    verification = begin_email_verification(db, user)
    db.commit()
    finish_email_verification(db, verification); db.commit()
    assert user.email_verified_at is not None
    with pytest.raises(AuthError): finish_email_verification(db, verification)
    remove_account(db, user, "long-password"); db.commit()
    assert user_for_token(db, token) is None
    assert user.email.endswith("@deleted.invalid") and not is_subscribed(user)


def test_stat_correction_is_auditable_and_consistent_across_record_views(db):
    from test_publication_pipeline import seed, row
    from spe.publishing.ledger import publish, settle, history, day_picks
    from spe.db.models import PublishedStat, PublishedResult, PublishedCorrection
    from app.queries import kickoff_day
    now = datetime.now(UTC)
    game = seed(db); game.kickoff_utc = now + timedelta(hours=1)
    quote = row(kickoff_utc=game.kickoff_utc.isoformat(), forecast={"prediction_as_of": now.isoformat(), "data_cutoff": now.isoformat()}, price_freshness={"observed_at": now.isoformat()})
    publish(db, [quote], now); db.commit()
    game.status = "final"
    stat = PublishedStat(key="g|p|rushing_yards", game_id="g", player_id="p", market_id="rushing_yards", actual=70, observed_at=now, source="test")
    db.add(stat); db.commit(); settle(db); db.commit()
    stat.actual = 20; db.commit(); settle(db); db.commit()
    assert db.query(PublishedResult).one().result == "win"
    assert db.query(PublishedCorrection).one().result == "loss"
    assert history(db)["rows"][0]["result"] == "loss"
    assert day_picks(db, date=kickoff_day(game.kickoff_utc))[0]["result"] == "loss"
    settle(db); db.commit()
    assert db.query(PublishedCorrection).count() == 1


def test_public_production_td_read_never_launches_model_or_network(db, monkeypatch):
    from app.main import combined_replay
    from spe.config import settings
    monkeypatch.setattr(settings, "app_env", "prod")
    def forbidden(*a, **kw):
        raise AssertionError("Public read started a worker")
    monkeypatch.setattr("spe.models.player_td.desk_replay.replay_day", forbidden)
    monkeypatch.setattr("spe.models.player_td.scorer_refresh.ensure_refresh", forbidden)
    monkeypatch.setattr("spe.models.player_td.desk_replay_results.ensure_live_touchdowns", forbidden)
    result = combined_replay(datetime.now(UTC).date(), "all", db, "card")
    assert result["refresh_status"] == "not_started" and result["rows"] == []


def test_scheduled_td_refresh_only_runs_upcoming_days(db, monkeypatch):
    from spe.publishing.pipeline import refresh_touchdown_cards
    now = datetime.now(UTC)
    games = [SimpleNamespace(kickoff_utc=now + timedelta(hours=hours)) for hours in (-12, 4, 96)]
    monkeypatch.setattr("app.queries.games_for_slate", lambda *a: games)
    calls = []
    monkeypatch.setattr("spe.models.player_td.desk_replay.replay_day", lambda *a, **kw: calls.append(a[1]) or {"rows": [], "unavailable_games": [{"game_id": "g"}]})
    assert refresh_touchdown_cards(db)["status"] == "degraded"
    assert len(calls) == 1


def test_live_scores_do_not_count_as_final_record_wins():
    from spe.publishing.ledger import _secondary_scorers
    row = {"game_id": "g", "player_id": "main", "game_status": "in_progress",
        "decision": {"value_candidate": {"player_id": "alt", "name": "Alternative"}},
        "decision_results": {"value_candidate": {"result": "scored"}}}
    assert _secondary_scorers(row)[0]["result"] == "pending"
    row["game_status"] = "final"
    assert _secondary_scorers(row)[0]["result"] == "win"


def test_alternate_final_result_updates_without_changing_saved_card(db):
    from copy import deepcopy
    from test_publication_pipeline import seed
    from spe.db.models import BoardSnapshot, PublishedStat
    from spe.models.player_td.desk_replay import day_card_key, saved_day_card
    from app.queries import kickoff_day
    game = seed(db); game.status = "final"
    day = kickoff_day(game.kickoff_utc)
    payload = {"rows": [{"game_id": "g", "player_id": "main", "retrospective": False,
        "kickoff_utc": game.kickoff_utc.isoformat(), "decision": {"value_candidate": {
            "player_id": "alt", "name": "Alternative", "price_supported": True, "american": 155}}}]}
    original = deepcopy(payload)
    stored = BoardSnapshot(key=day_card_key(day, "all"), payload=payload, updated_at=datetime.now(UTC))
    db.add_all([stored, PublishedStat(key="g|alt|anytime_td", game_id="g", player_id="alt", market_id="anytime_td", actual=1,
                                    observed_at=datetime.now(UTC), source="espn.summary:test")])
    db.commit()
    result = saved_day_card(db, day)
    assert result['decision_record']['value_candidate']['wins'] == 1
    assert result['decision_record']['value_candidate']['hypothetical_unit_profit'] == 1.55
    assert stored.payload == original


@pytest.mark.parametrize("existing", [False, True])
def test_alembic_adopts_existing_database_without_losing_users(tmp_path, monkeypatch, existing):
    from alembic.config import Config
    from alembic import command
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from spe.config import settings
    from spe.db.base import Base
    from spe.db.models import AccountUser
    url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine = create_engine(url)
    if existing:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            register_user(db, "preserved@example.com", "long-password")
            db.commit()
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE email_verifications"))
            conn.execute(text("ALTER TABLE account_users DROP COLUMN email_verified_at"))
            conn.execute(text("ALTER TABLE account_users DROP COLUMN deleted_at"))
    command.upgrade(Config("infra/alembic.ini"), "head")
    command.upgrade(Config("infra/alembic.ini"), "head")
    with Session(engine) as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261116_04"
        assert db.query(AccountUser).count() == int(existing)
    engine.dispose()
