# Published opportunities and cloud refresh

The public prop card now serves only selections saved before kickoff. Qualification uses a verified quote, a known bookmaker and outcome, model/data timestamps, confirmed availability, input-quality flags, and the configured minimum EV after uncertainty (default 4%). Research forecasts do not become validated Play.

The first qualifying prop per game/market is inserted into `published_picks`. Original price, side, line, model, probability and publication time never change. A current card shows it only when that same book, selection and price is freshly verified and both the frozen and current forecast pass eligibility. A price change can remove it from the live card; its original result remains in the ledger. Research scorer rankings and experimental parlays are identified separately. ATS history uses the original frozen current-cohort research records, never the changing board.

Settlement uses explicit market-specific ESPN summary results. Return touchdowns count for anytime TD; passing TDs do not. Missing markets remain pending. Canceled games are void. Unconfirmed DNPs remain pending for review instead of guessing a loss or void. Original settlements remain immutable. Later supported source revisions append corrections used by the daily, monthly and yearly ledger. ROI risks one unit per selection, includes pushes in stakes and excludes voids/pending. This is research accounting, not a guarantee of a bookmaker's settlement rules.

## Release and first run

1. Review and merge the code, then deploy API and workers together. Existing tables are retained; run `python -m spe.db.release` as the API pre-deploy command. Production `init_db` verifies Alembic revision `20261116_04` and never performs ad hoc schema changes. Take a database backup before production rollout and verify it is restorable.
2. For the existing Michael workspace, use `infra/deploy/render-production-jobs.yaml` with existing-resource adoption. It targets `caliber-hourly` and `caliber-scoreboard`, includes `caliber-inputs` and `caliber-touchdowns`, and references the existing `caliber-db-xtkn` database and `caliber-api-xtkn` credential values without copying secrets into Git. The API stays on its existing service and deploys from master. Do not create duplicate API/database resources from the generic root template. For a new independent installation, provision the API/database with `render.yaml`, then adapt the production jobs file to those resource names. Keep the existing `caliber-hourly` service name so the old hourly job is updated, rather than accidentally leaving duplicate odds jobs enabled. Set `ODDS_API_KEY` on the odds job, `API_SPORTS_KEY` and an identifying `NWS_USER_AGENT` on the input job. `API_SPORTS_KEY` is absent in the current cloud API settings, so job creation deliberately does not depend on copying it. This optional paid college injury source can remain unavailable without failing successful free-source refreshes. Its missing coverage stays explicit; it never means a player is healthy. All Render jobs and the API must use the same private/internal Postgres connection string. Do not use the external hostname for same-region workers.
3. Run each lane once in the deployed environment:
   - `python -m worker.supervise --lane inputs --timeout 720`
   - `python -m worker.supervise --lane odds --timeout 240`
   - `python -m worker.supervise --lane settlement --timeout 720`
   - `python -m worker.supervise --lane touchdowns --timeout 240`
4. Verify `/v1/operations/status`, `/v1/props/picks`, and `/v1/published/history`. Empty published history is expected initially; no historical picks are backfilled as if published live. The research boards warm into Postgres on the first odds cycle. Production requests never rebuild heavy boards or silently serve baked JSON caches.
5. Keep `/health` as process liveness. Configure an external HTTP uptime monitor against `/ready`, which returns 503 for missing, failed, or overdue jobs. The in-app banner surfaces these failures; external email/SMS delivery requires configuring the hosting/monitoring destination.

## Cadence and failure handling

- Odds, forecasts, boards and publication: every 5 minutes. The Odds API's existing adaptive polling and daily credit budget still apply. A budget stop is a visible degraded run; the budget is not increased automatically.
- TD picks and simulations: every 5 minutes on the separate touchdown lane.
- NFL/FBS availability and weather: every 15 minutes. A missing key or blocked plan stays visible and never means healthy.
- Scores, published box results, research and published settlement: every 15 minutes.
- Postgres advisory locks prevent overlapping copies of each lane. Failed steps roll back their work and record failure separately. Process crashes leave a running heartbeat that becomes overdue. Worker failure exits nonzero for hosting alerts.
- Input stages run in separate child processes under one parent advisory lock and the supervisor deadline. Each complete NFL source is committed before the next; a later failure cannot erase earlier successful inputs. Test a forced full NFL plus college refresh on the actual 512 MB worker size, not only a warm `not_due` run.
- API and workers share `board_snapshots` and checksum-verified compressed `input_artifacts`. Local JSON is retained only for explicit fixture paths; bundled snapshots are not the production cache.

Test the scheduled lanes with real credentials on staging before a live release. This change does not configure hosting accounts, purchase provider plans, enable subscription billing, or establish a profitable model track record.

## Netlify deployment fixes

Fonts are bundled from pinned Fontsource packages so builds do not rely on Google Fonts downloads. The explicit Next.js API proxy replaces the old external rewrite. Current prices, picks, parlays, health, and any session-authenticated response use no-store; only the anonymous day-list response has a bounded 30-second browser cache.

## Release verification

Verify the exact Git commit on the API, all four worker services, and the published Netlify deploy. Passing CI is required, but does not prove an auto-deploy hook fired. If a hook fails, disable automatic deployment before triggering a manual deployment, then restore the setting. Check actual completed job records and `/ready` after rollout; a green deploy alone does not verify data refreshes. Do not alter saved selections or retrospectively replace losses during a release.
