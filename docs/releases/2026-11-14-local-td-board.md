# Local Anytime TD board repair

Local startup now loads shipped Desk model weights when no custom artifact exists.
Opening Today or Anytime TD in development starts a background rebuild when the
saved board is missing, expired, or built with different primary model weights.
The page polls until the rebuild finishes and stays on the selected date. Changing
league or date resets the Anytime TD view so previous results are not relabeled.

Pregame decisions and board snapshots now write through the same database session
and commit together. This removes the SQLite writer-lock conflict that prevented
publication. The production API still leaves publication to its scheduled worker.
Rebuilding only uses data already in the database; it does not fetch paid data or
invent missing player history.

Verification used a local SQLite copy of real schedules, player history, rosters
and quotes. With snapshots cleared, the actual Next.js site and API rebuilt 795
CFB rows for November 14 and 274 NFL rows for November 15. The Next.js proxy
returned the rows, the homepage rendered cards, and game-detail probabilities
matched the board. This verifies availability, not predictive performance or
current price validity. No production database was modified.

Checks: 169 backend tests, 10 frontend logic tests, and the Next.js production
build. Browser interaction was unavailable because this environment denied the
browser's socket creation; the running-site check used HTTP and rendered HTML.
