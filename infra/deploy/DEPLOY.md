# Current production pipeline

For the publication ledger, shared database boards, current cron schedules, and deployment checks, follow [PRODUCTION_PIPELINE.md](PRODUCTION_PIPELINE.md). The older setup notes below describe the original prototype; its cron table and cost estimates are superseded.

# Deploy Caliber (simple)

You already have **Netlify** (website) and **API keys**.  
This adds the missing pieces: **API + database + scheduled chores**.

```
People → Netlify (website)
              ↓
         Render API
              ↓
         Postgres DB
              ↑
         Render cron (odds / freeze / settle)
```

## 1. Render (API + DB + worker clock)

1. Push this repo to GitHub (if it isn’t already).
2. Go to [render.com](https://render.com) → **New → Blueprint**.
3. Select the repo. It should read `render.yaml`.
4. Create the services. Free/starter plans are fine to start.
5. In **caliber-api** env vars, set:
   - `WEB_ORIGIN` = your Netlify URL (example `https://your-site.netlify.app`)  
     Comma-separate if you have more than one (`https://a.netlify.app,https://b.netlify.app`).
   - `ODDS_API_KEY`
   - `CFBD_API_KEY`
6. Wait until `https://caliber-api.onrender.com/docs` loads (name may differ).

Cron jobs in the blueprint:

| Job | What it does | When |
| --- | --- | --- |
| `caliber-scoreboard` | ESPN scores / finals | every 30 min |
| `caliber-odds-refresh` | live prop prices | every 3 hours |
| `caliber-ops-cycle` | freeze → settle → desk takes | every 4 hours |

Official Play stays **off**. These jobs only keep research data fresh.

## 2. Netlify (website)

1. Site settings → **Build & deploy**
   - Base directory: `apps/web`
   - Build command: `npm run build`
   - Plugin: `@netlify/plugin-nextjs` (from `netlify.toml`)
2. Site settings → **Environment variables**
   - `API_URL` = your Render API URL (no trailing slash)
   - `NEXT_PUBLIC_API_URL` = same URL
3. Trigger a redeploy.

## 3. Smoke check

1. Open the Netlify site.
2. Today / Anytime TD should load (not empty forever).
3. Open Render cron logs after a few hours — odds + ops should show `status: ok`.

## Local Docker (optional)

```powershell
docker compose up --build db api
# one-shot chores:
docker compose --profile jobs run --rm worker
```

## Cost (starter)

| Piece | Ballpark |
| --- | --- |
| Netlify | $0–20 |
| Render API + cron + Postgres | $15–40 |
| Odds + CFBD keys | $5–45 |
| **Total** | about **$25–80 / month** |

## If something fails

- Netlify loads but data empty → `API_URL` wrong or API asleep (Render free tiers spin down; upgrade or hit `/docs` to wake).
- CORS errors → set `WEB_ORIGIN` to the exact Netlify URL (https, no trailing slash).
- Worker crashes on DB → confirm `DATABASE_URL` is linked from `caliber-db`.
