# Local scoring-case engine

This release adds a measured NFL scoring-path allocation, a separate price decision,
and saved pregame records for both strategies. It does not establish better accuracy.
NFL v10 is a local, date-scoped experiment. FBS keeps its separately fitted model.

## Data and prediction

`analytics/td_scoring_paths.py` partitions valid public nflverse carries and targets
into disjoint field-position/target-depth bins. Estimated end-zone targets use air
yards, not charted coverage. Unknown fields retain their own bins. Passing TDs
belong to the receiver, never the quarterback's anytime outcome.

`models/player_td/scoring_paths.py` aligns players to six prior team games, retains
missed team games, and blends last-two shares with longer history. It estimates
opportunities from offensive and defensive volume, shrinks player and opponent
position conversion samples toward fit-period league rates, and allocates the
existing fitted rushing/receiving team TD budgets across competing teammates.
Half of absent same-position work is reserved for uncertain replacement roles.
No-inheritance, full-inheritance, recent-role and longer-role scenarios accompany
the central projection. The range is sensitivity analysis, not a confidence interval.

The existing player/team model includes historical efficiency, workload, game
spread/total and other fitted inputs. The new path model is combined with it using
two nonnegative logit coefficients fitted before the evaluation window. Weather,
NGS weekly summaries, current quarterback/line/defender reports and depth charts
remain explicitly contextual where an effect was not fitted. Storing data does
not imply it changes the probability.

## Decision, display and grading

`scoring_decision.choose` considers all eligible players on both teams. Policy v3
always makes the highest model scoring chance the main pick, regardless of the
individual player's bookmaker odds. A separate, distinct better-priced
alternative needs all of: a measured current role, a specific scoring-area route
or supported deep-receiving matchup, a fresh price better than the main pick's,
and positive value across the declared role scenarios and component comparison.
No price-only watchlist is published as a pick. The highest central scoring chance
among the qualifying alternatives wins that second slot. There is no position
quota, five-point switch cap, or 21-point team exclusion.

`football_case.assess` uses declared editorial evidence requirements rather than
altering a probability: two measured recent team games with offensive work; at
least two recent scoring-area opportunities, a smoothed recent share of 10% and
at least 0.25 projected opportunities in that area; alternatively, a measured deep
role meeting an above-benchmark defensive allowance with ten defensive samples.
Unknown or sharply diminished roles and unresolved questionable designations
cannot qualify the alternative. These rules have not demonstrated higher returns.

The new selection policy applies to fresh pregame decisions for both leagues.
Probability model defaults remain governed separately by their validation gates.
The detailed v10 NFL probability model remains date-scoped; a model/league without
the required measured path evidence does not get an invented value alternative.
Cached pregame responses recheck playing status for both choices.

`customer_explanation.build` creates readable role, matchup, caution and odds
paragraphs for both names. `scoring_case.build` creates the detailed argument and price
verdict from saved inputs, without looking at results. `desk_replay.replay_day`
and `pregame_candidates.save_candidates` save both strategy names before kickoff.
Completed snapshots are frozen; results update independently. The primary Desk
card, most-likely lane and conditional-value lane have distinct records. Watchlist
players are not counted as value selections. Hypothetical unit returns are computed
only for supported saved prices and are not claims about bets actually placed.

## Training and results

Run from the repository with `PYTHONPATH=packages/core;services/api;scripts;.`:

```powershell
.venv\Scripts\python.exe scripts/train_td_paths.py --database data/caliber.db --output data/research/td_paths_v10
.venv\Scripts\python.exe scripts/run_td_scoring_case.py --date 2026-11-15 --model data/research/td_paths_v10/nfl_desk_combined_v10_candidate.json --context-file data/research/td_paths_v10/context_2026.json --output <local-output-folder>
```

The second command requires an entirely future slate, archives the previous
snapshots, pins the artifact SHA, and restores the prior date configuration if
the rerun cannot produce a complete eligible board. It changes no deployment or
global model default. Normal nflverse ingestion now generates the new bins too.

Priors: games before 2025-10-15. Component calibration: 2025-10-15 through
2025-12-14. Validation: 2025-12-15 through 2026-02-14. Nov 14, 2026 outcomes are
excluded. On 63 matching validation games / 1,267 player-games: v10 25 hits,
v9 25, older v2 26. Brier: .134600, .135162, .134217 respectively. On the previously
examined 105-game 2026 sample: 54, 54, 55 hits. No promotion gate passed.
These are highest-chance scorer comparisons, not the new price policy's hit rate.
Recorded historical player prices are insufficient for a fair ROI comparison.

Historical data has revisions and an assumed 48-hour availability lag. Historical
evaluation is limited to observed participants with sufficient history; it does
not prove complete pregame/DNP coverage. Earlier design work inspected these
seasons. A new prospective evaluation is required. Do not retune this model to
turn already-seen losers into winners.

## Remaining evidence gaps

- Current comprehensive routes, first-read share, receiver man/zone/safety-shell
  splits and defender assignments are not supplied by the configured free feeds.
  nflverse FTN participation is released after the season and is excluded under
  this project's existing license policy. Public isolated article facts do not
  provide a complete, historically testable weekly dataset.
- Injured blocker/defender replacement quality and an upcoming coach's actual
  game plan are not verified numerical model inputs.
- Debut players without prior role history may still be missing from the pool;
  do not mistake a recent-history pool for a complete current depth chart.
- The detailed NFL path calibration must not be reused for college football.

Sources: public nflverse PBP; PFR snaps and NGS weekly summaries via existing
nflverse adapters; existing public ESPN availability; existing recorded odds.
No new purchase, deployment, copied Sharp picks, or forced position mix.
