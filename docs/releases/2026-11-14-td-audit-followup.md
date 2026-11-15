# Anytime TD report follow-up

## Serving and selection

The Desk now scores eligible quoted players directly from database history. It no
longer requires a pre-existing M3 forecast or an old college finder file. All Anytime
TD rows must receive an opportunity prediction before appearing. Missing current
role history remains a reason to withhold a candidate, not a fabricated probability.

Game detail reads the exact published board snapshot, including probability, model
version, cutoff, selected status and quote. Its disagreement calculation uses that
same probability. Started games use the recorded pregame call; no historical choice
is recomputed. Fresh-price and availability checks remain mandatory for selection.

Current game-specific injury reports override stale forecast status. Reports observed
or reported after the scoring cutoff are excluded. Confirmed out/inactive players get
zero probability and cannot be selected. Unknown availability remains disclosed.

## Historical evaluation repairs

- Player TD evaluation builds an unfitted feature frame. Each chronological fold fits
  its own upstream team model and share weights using fitting games only.
- Calibration games follow fitting games and precede final evaluation games. Game
  kickoffs remain together at boundaries; overlap raises an error.
- Team model hashes, fit cutoffs and row counts are reported. Newly trained player
  artifacts contain the paired team model, which their live research scorer uses.
- Current saved M3/M4 artifacts are excluded as historical comparison baselines when
  their training vintages cannot be established. Their bytes are never overwritten.
- Role calibration no longer accepts or deletes shifts based on final-test results.
- The NFL v3 candidate now has separate fitting, calibration and validation periods.
  It remains shadow-only. Its 2026 top-scorer replay is 49/104 versus v2's 50/104.
  The old v2 calibration overlaps the diagnostic validation period; that comparison
  is not an independent head-to-head. No profitability improvement is claimed.
- Poisson count variance is no longer presented as validated probability uncertainty.
  Research intervals are explicitly unvalidated.

## Allocation repairs

The team/share research model removes confirmed inactive teammates before allocation.
Measured backup opportunities receive the remaining share. Zero-opportunity players
receive no invented equal workload; pure passing QBs receive no receiving allocation.
This research change does not silently replace the fitted live Desk model.

## Still requires evidence or additional data

The original report was not solely a bug list. These items are not claimed complete:

- A CFB player-level PBP opportunity challenger with audited player IDs, transfers,
  nullified plays and coverage. NFL feeds cannot provide college routes or goal-line
  opportunities. Existing measured college carries/receptions remain the live inputs.
- Reliable current injury coverage. The existing API-Sports college injury source is
  unconfigured in production; ESPN availability is partial and does not prove health.
- Tested replacement-player workload forecasts where same-team history is missing.
  The Desk withholds those candidates instead of treating guessed workload as fact.
- Independent prospective comparison, game-clustered calibration intervals and a
  sufficiently large record with executable pregame prices. Revised historical stats,
  incomplete historical injury vintages and participant-conditioned cohorts remain
  limitations. Historical hit rate does not establish betting profit.

No code from the unlicensed comparison repository was copied.

## Verification

Regression checks cover future-label perturbation, independent calibration, complete
game splits, fold orchestration, paired model persistence, inactive redistribution,
live injury cutoff behavior, missing legacy forecasts, published detail parity and
frozen started-game calls. Existing pipeline, feed, selection and frontend tests pass.
The free-data integration and 502-player file/database feature parity were also run.
