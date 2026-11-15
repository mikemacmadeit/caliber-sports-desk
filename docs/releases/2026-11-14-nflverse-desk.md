# Free nflverse inputs for the Anytime TD Desk

The Desk previously ignored current PFR snap counts and most available play-level
opportunity detail. Historical imports skipped corrected box scores and updated
schedules. The new refresh runs in the existing inputs worker, with no new provider
subscription or service. Successful refreshes are six hours apart; degraded refreshes
retry after one hour. `refresh-nflverse-desk --year 2026 --force` runs it explicitly.

## Inputs and behavior

- PFR snap counts via nflverse, joined by exact PFR-to-GSIS identifiers. Unresolved IDs
  remain missing. Snap share and recent changes describe playing time, not routes run.
- Free PBP supplies carries and targets inside the 5/10/20, team opportunity shares,
  targets per dropback, distinct red-zone drives, neutral pass rate, passing/rushing EPA,
  and opponent opportunities allowed by position. Nullified plays, conversions, spikes,
  and kneels are excluded; sacks count as dropbacks but not receiving targets.
- End-zone targets are estimates from air yards and field position, explicitly labeled.
  Missing air yards remain unknown; at least 90% coverage is needed for a player-game
  to contribute to the end-zone feature. These are not FTN/SIS film-charted targets.
- NGS weekly receiving/rushing/passing summaries and dated depth charts are refreshed.
  Recent, same-team separation and rushing efficiency appear in the explanation.
  Depth rank is context, never an automatic penalty or an injury confirmation.
- New game-context tables persist measured inputs and provenance in Postgres. Only
  final games at least 48 hours old are admitted. Actual observation time also gates
  live reads. The 48-hour historical lag is an assumption; revised files are not an
  archive proving what was available before a historical kickoff.
- The original opportunity model receives corrected measured goal-line/red-zone
  counts. Extra measurements appear in reasons and source attribution. Missing recent
  inputs are disclosed. NFL feeds never fill CFB gaps.

Empty/failed feeds preserve prior data. Dataset savepoints prevent a failed later source
from undoing a successful earlier import. NGS updates preserve other stat types; recent
depth refreshes retain older snapshots. Complete game snapshots apply corrections,
including decreases and disappearing player rows. Schedule updates promote completed
games to final without downgrading finals when a later source omits the score.

## Participation timing and licensing

nflverse participation from 2023 onward is supplied after the postseason. It is no
longer used as an in-season source by the usage builder, including previously imported
rows with an overly early timestamp. New automatic imports exclude these FTN files.
The separate free FTN charting subset remains excluded under the repository's existing
license policy. This is not a claim that share-alike licensing prohibits commercial use.
See `ATTRIBUTION.md` for source distinctions and documentation links.

## Model comparison and rollout

A separately fitted NFL v3 candidate adds the free features. Both live and replay paths
use the same transformations and feature code, with v2 fallback where recent snap/PBP
coverage is incomplete. Training ends November 1, 2025; November 2025 games calibrate; December 2025 through February 2026
games validate; 2026 games are a retrospective evaluation, not a betting record.

| Probability-only 2026 replay | Existing v2 | Candidate v3 with fallback |
|---|---:|---:|
| Scorer hits | 50 / 104 | 49 / 104 |
| Brier score (lower is better) | 0.14805 | 0.14833 |
| Log loss (lower is better) | 0.46278 | 0.46244 |

The candidate slightly improves validation probability metrics but loses a top-scorer
hit and does not demonstrate an overall improvement on 2026. The calibration and
validation periods are separate. The older v2 baseline was calibrated across this
validation period, so that comparison is diagnostic, not an independent head-to-head. Its artifact therefore has **`live_enabled: false`**.
The primary predictor remains v2. Shadow research approval permits measurement, not promotion. Candidate estimates and artifact hashes are frozen
for available players alongside each pregame decision, including passes, for prospective
comparison. They do not replace the primary pick or count as wins in the Desk scorecard.
Validated Play remains off. No hit-rate or profitability improvement is claimed.

Reproduce with `scripts/evaluate_nflverse_desk.py` using the compact historical rows,
downloaded 2025/2026 PBP and snap files, players file, and the shipped v2 artifact.
Aggregate metrics and source counts are in `2026-11-14-nflverse-evaluation.json`.
No raw provider files are committed.

## Verification

Real 2026 files imported into an isolated database: 12,672 snap records, 2,869 PBP
context records, 1,391 NGS rows, 30,281 recent depth-chart rows and 24,554 roster rows.
Database and file-based replay features matched for 502 eligible players. Focused
backend tests cover opportunity semantics, cutoff leakage, IDs, corrections, partial
failures, preservation, throttling, frozen comparisons and primary-model protection.
Frontend selection/expiry tests and the production Next build also pass.

The existing inputs worker must run this release before new inputs appear in production.
Deploy verification is separate from the local checks above.
