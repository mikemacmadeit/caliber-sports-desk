# Research

League-specific notebooks and experiment tracking live here.

Rules:

- NFL and FBS models never share calibrated parameters.
- Rolling-origin evaluation only. Do not tune on the test fold.
- Store prospective forecasts in the recommendation ledger before optimizing.
- Ablate weather, injuries, home field, recent form, market anchor, opponent-adjusted defense, personnel, and coaching. Drop complexity that fails held-out tests.
- MLflow (or equivalent) belongs here once training leaves the prototype hierarchical models.

Suggested first notebooks (not committed until they have real data):

1. `01_nfl_novig_baseline.ipynb`
2. `02_fbs_novig_baseline.ipynb`
3. `03_hier_td_counts.ipynb`
4. `04_leakage_audit.ipynb`
5. `05_calibration_curves.ipynb`
