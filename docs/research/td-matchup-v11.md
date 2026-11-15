# NFL matchup research, 2026-11-15

The Desk now retains a highest-probability scorer and a separately ranked football alternative. The latter is a scorer recommendation even when its last quote expires, not an automatically qualified bet. Passing touchdowns remain receiver outcomes.

## Connected implementation

- `analytics/td_scoring_paths.aggregate`: PBP v2 records exact inside-20 targets by passer, alongside estimated end-zone targets, position allowances and leading/close/trailing workloads. The inside-20 audit total is excluded from the mutually exclusive scoring bins, preventing double-counted touchdowns.
- `models/player_td/matchup_research`: uses the current depth-chart QB when unique and eligible, otherwise explicitly labels the last game's dominant passer. Requires two measured games and 30 team targets before conditioning receiving shares. A new or unresolved starter receives no borrowed prior-QB split. Records counts, ranks, sample sizes and limitations.
- `scoring_paths.project`: v11 blends the existing receiving share with a QB-specific posterior (eight team opportunities of prior weight, half-weight). Team rushing/receiving TD budgets and unknown-player reserves remain conserved. v10 behavior is retained under its original artifact version.
- `scoring_decision.choose`: main pick maximizes calibrated probability. Alternative needs current work, measured scoring opportunity and a concrete role/matchup advantage. Fixed editorial score: 50% scoring probability, 20% scoring-area opportunity capped at two, 15% supported opponent advantage, 10% leading QB red-zone role, 5% rising role or demonstrated game-state fit. This score is not a probability and was not tuned to today's outcomes or Sharp's names. A known alternative quote must pay more than the main's reference quote; unknown/expired odds receive explicit labels.
- `customer_explanation.build`: player-specific QB counts, opportunity-share changes, defensive yards/TD allowances, game-state work and comparison with another teammate. All prose comes from saved pregame inputs. It does not invent route or coverage data.
- `desk_replay`, `desk_replay_results`: both selected scorers grade separately. An alternative with no fresh qualifying quote cannot contribute hypothetical betting profit. Started snapshots remain frozen even when the local default artifact changes.
- `scorer_refresh`: local free NFL input refresh respects the existing six-hour due check, commits source updates before model work, and coalesces source refreshes. Quotes use the separately configured bounded odds ingestion; no purchase or budget increase.

## Persistence and scope

November 15 uses the hash-pinned v11 candidate. `desk_retrain_runs.json` also records a user-requested **local development** default from November 16. Explicit dated choices win. The local default is ignored outside `app_env=dev`; no production promotion or deployment occurred. A standing local model uses the stable pregame snapshot key so later artifact changes cannot rewrite a started pick.

If a champion without calibrated path probabilities is used, the research layer can still rank an alternative without changing that champion's probability or claiming scenario-tested betting value. College keeps its independently trained model; no NFL research inputs are transplanted into FBS.

## Validation

`scripts/train_td_paths.py --contextual` fits only calibration coefficients on the original calibration period. The model design is fixed, not selected by this day's results. On the matching 63-game validation slice: v11 25 winners, v10 25, v2 26; Brier v11 .13463, v10 .13460, v2 .13422. On the already-examined 2026 105-game sample: v11 55, v10 54, v2 55. The promotion gate **failed**. This does not establish a general accuracy improvement.

Historical samples use revised PBP, an assumed 48-hour availability lag and observed participants; complete pregame injury/depth snapshots and executable historical prices are missing. The editorial alternative ranking has no demonstrated prospective accuracy or ROI advantage. Freeze future candidate sets and compare probability calibration, main/alternative hit rates and only price-qualified returns separately.

## Unavailable information

Current full-route participation, receiver efficiency against man/zone/blitz/safety coverages, and verified future play calls remain unavailable. Snap share and targets are not routes. Individual defender on/off effects are not fabricated. Weather and some NGS summaries remain contextual rather than learned adjustments.

Source: nflverse PBP, PFR snaps via nflverse, public ESPN availability, nflverse depth/roster/NGS summaries. FTN participation is postseason-delayed; the separate FTN charting subset remains excluded under `ATTRIBUTION.md`. See https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html and https://nflreadr.nflverse.com/reference/load_ftn_charting.html . Sharp's anytime sections are a research-format reference, not training labels: https://www.sharpfootballanalysis.com/betting/nfl-first-touchdown-scorer-bet-picks-prediction/ . Different source definitions/windows can produce different counts; no claim of matching its proprietary research or accuracy is made.
