# Caliber

Odds-aware **calibrated probability engine** for **NFL** and **NCAA Division I FBS**. Private research tool first; commercial subscription only after validation and written data rights.

This is not a picks generator, not a sportsbook, and not an automated betting bot. It produces timestamped probabilities, uncertainty intervals, best available prices, no-vig baselines where identified, expected value vs the offered price, and source-backed explanations. **No bet** is a first-class outcome.

## What v0.1 ships

Monorepo matching the implementation blueprint:

| Path | Role |
| --- | --- |
| `apps/web` | Next.js dashboard (Today, Game Deep Dive, Anytime TD, Props, Scenario Lab, Parlay Evaluator, Track Record, Data Quality) |
| `services/api` | FastAPI `/v1/*` contracts |
| `services/worker` | Ingest jobs with Odds API credit ceilings |
| `packages/core` | Canonical schema, adapters, identity, market math, hierarchical models, simulator, recommenders |
| `packages/contracts` | OpenAPI + unit/schema registry |
| `research` | Rolling-origin notebooks (empty until live data) |
| `infra` | Docker Compose, Alembic, seed slate |

Mandatory launch coverage is **both leagues** with equal product priority. Models do **not** share calibrated parameters. College injury gaps never default to healthy.

v1 markets: anytime TD, receptions, rushing yards, receiving yards.

## Explicit exclusions

Real-money wagering, automated bet placement, scraping protected sportsbooks, live in-play betting, NFL tracking-data licensing, unverified injury rumor adjustments, and commercial redisplay of vendor feeds without a contract.

## Deploy (Netlify website + Render API/worker)

Local laptop optional. Cloud setup:

- `render.yaml` — Postgres + API + cron chores (odds / freeze / settle)
- `netlify.toml` — Next.js website
- Step-by-step: [`infra/deploy/DEPLOY.md`](infra/deploy/DEPLOY.md)

## Quick start (local, no vendor keys)

Python 3.12+ and Node 22+.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\python .\infra\seed\seed.py
$env:PYTHONPATH = "packages\core;services\api"
.\.venv\Scripts\python -m uvicorn app.main:app --app-dir services\api --port 8000
```

In another terminal:

```powershell
cd apps\web
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). API docs: [http://localhost:8000/docs](http://localhost:8000/docs).

The seed slate is synthetic demonstration data for the weekend of 7–8 November 2026 so the UI works before CFBD / Odds API keys exist.

```powershell
.\.venv\Scripts\pytest
```

## Vendor keys (optional live ingest)

Copy `.env.example` to `.env`.

| Vendor | Initial tier | Use |
| --- | --- | --- |
| CollegeFootballData | Tier 2 (~$5/mo) | FBS schedules, PBP, EPA/PPA |
| The Odds API | Free → 20K (~$30/mo) | NFL + NCAAF player props. **Credits ≠ requests.** |
| nflverse | Free CC-BY releases | NFL PBP / stats. Attribute; avoid FTN/CC-BY-SA features. |
| NWS | Public | Kickoff forecasts. Unique User-Agent required. |
| Open-Meteo | Evaluation only | Noncommercial prototype; commercial license required later. |

See `ATTRIBUTION.md`. Commercial launch is blocked until counsel and contracts cover odds, stats, identities, weather, trademarks, advertising, age, geo, and privacy.

## Architecture

```
stats/odds/injuries/weather → append-only raw store → identity + event-time features
  → league-specific models → correlated simulation → calibration vs no-vig
  → FastAPI + Next.js → immutable recommendation ledger → settlement / monitoring
```

Point-in-time rule: `observed_at` is when the fact was available to us. Never treat `ingested_at` or future revisions as historically known.

## Strategic rules encoded in code

- Calibration beats simulation volume.
- A recommendation is tied to a timestamped price, not a player name.
- Missing/stale injury coverage lowers confidence; it is never “healthy.”
- “Due for a TD” is not a feature unless an ablation proves held-out value.
- Parlays use the same draw set; independent products are a flagged fallback.
- NFL results cannot conceal weak FBS results.

## Next backlog

1. Live CFBD + nflverse historical import with leakage tests.
2. Empirical Odds API credit measurement on selected books/markets.
3. As-of backtest harness on real seasons (independent NFL and FBS reports).
4. Weather/injury snapshots on gameday cadence within budget.
5. Four-plus weeks of prospective shadow tracking before any commercial feed.
