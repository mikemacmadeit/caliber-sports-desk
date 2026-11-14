# Published opportunities and cloud refresh

The public prop card now serves only selections saved before kickoff. Qualification uses a verified quote, a known bookmaker and outcome, model/data timestamps, confirmed availability, input-quality flags, and the configured minimum EV after uncertainty (default 4%). Research forecasts do not become validated Play.

The first qualifying prop per game/market is inserted into `published_picks`. Original price, side, line, model, probability and publication time never change. A current card shows it only when that same book, selection and price is freshly verified and both the frozen and current forecast pass eligibility. A price change can remove it from the live card; its original result remains in the ledger. Research scorer rankings and experimental parlays are identified separately. ATS history uses the original frozen current-cohort research records, never the changing board.

Settlement uses explicit market-specific ESPN summary results. Return touchdowns count for anytime TD; passing TDs do not. Missing markets remain pending. Canceled games are void. Unconfirmed DNPs remain pending for review instead of guessing a loss or void. Final recorded settlements are not automatically rewritten for later source revisions. ROI risks one unit per selection, includes pushes in stakes and excludes voids/pending. This is research accounting, not a guarantee of a bookmaker's settlement rules.

## Release and first run

1. Review and merge the code, then deploy API and workers together. Existing tables are retained; `init_db` creates the new additive tables. Take a database backup before production rollout and verify it is restorable.
2. Apply `render.yaml`. Keep the existing `caliber-hourly` service name so the old hourly job is updated, rather than accidentally leaving duplicate odds jobs enabled. Set `ODDS_API_KEY` on the odds job, `API_SPORTS_KEY` and an identifying `NWS_USER_AGENT` on the input job. All jobs and the API must use the same Postgres database.
3. Run each lane once in the deployed environment:
   - `python -m worker.main production-cycle --lane inputs`
   - `python -m worker.main production-cycle --lane odds`
   - `python -m worker.main production-cycle --lane settlement`
4. Verify `/v1/operations/status`, `/v1/props/picks`, and `/v1/published/history`. Empty published history is expected initially; no historical picks are backfilled as if published live. The research boards warm into Postgres on the first odds cycle. Production requests never rebuild heavy boards or silently serve baked JSON caches.
5. Keep `/health` as process liveness. Configure an external HTTP uptime monitor against `/ready`, which returns 503 for missing, failed, or overdue jobs. The in-app banner surfaces these failures; external email/SMS delivery requires configuring the hosting/monitoring destination.

## Cadence and failure handling

- Odds, forecasts, boards and publication: every 5 minutes. The Odds API's existing adaptive polling and daily credit budget still apply. A budget stop is a visible degraded run; the budget is not increased automatically.
- NFL/FBS availability and weather: every 15 minutes. A missing key or blocked plan stays visible and never means healthy.
- Scores, published box results, research and published settlement: every 15 minutes.
- Postgres advisory locks prevent overlapping copies of each lane. Failed steps roll back their work and record failure separately. Process crashes leave a running heartbeat that becomes overdue. Worker failure exits nonzero for hosting alerts.
- API and workers share `board_snapshots`. Local JSON is retained only for explicit fixture paths; bundled snapshots are not the production cache.

Test the scheduled lanes with real credentials on staging before a live release. This change does not configure hosting accounts, purchase provider plans, enable subscription billing, or establish a profitable model track record.
