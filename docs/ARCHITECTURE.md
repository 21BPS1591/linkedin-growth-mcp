# Architecture

## Components

- **`server.py`**: FastMCP tools (about 60) and, in HTTP mode, the APScheduler jobs: an hourly autopilot cycle, a daily posting-hour cycle, a 6-hourly metrics refresh and a 2-minute check for owner-scheduled posts.
- **`service.py`**: the facade every tool calls. Holds the Gemini clients (one key per kind of work), the browser, and the store.
- **`autopilot.py`**: decides what to post and when. IPO posts are ranked by `ipo_quality` and capped per week; other days use `topic_order` (weekly mix with a results nudge and a 30-day floor). Each draft goes through `propose_post`, `polish`, the gates, an editor round and then publishing.
- **`strategy/`**: pure, testable logic.
  - `gates.py`: deterministic checks (`check_post`, `check_general`, `ungrounded_numbers`, `banned_term_hits`).
  - `bandit.py`, `styles.py`: Thompson-sampling over style x hook arms.
  - `experiments.py`: concurrent format and slot trial with a stratified permutation test.
  - `topics.py`: weekly topic mix and validation of auto-generated ideas.
  - `hooks.py`, `aitells.py`, `ownvoice.py`, `formulas.py`: opening linter, regex AI-tell score, voice fingerprint and profile scorecard, structure hints.
- **`gemini/`**: text and image clients with per-key daily caps and a rate guard. A job without its own key pauses instead of borrowing another key.
- **`linkedin/`**: official API publishing; a read-only Patchright browser for analytics and profile text.
- **`ipo/`**: filing (RHP) extraction, closest-peer comparison and market-data enrichment.
- **`store/db.py`**: SQLite. Tables for drafts, posts, metrics (append-only), ideas, conversation quality, key/value settings and the autopilot log.

## Data flow for one post

1. Pick the day's cell from the trial (format and slot) and the topic group.
2. Build the fact pack: filing facts for IPO posts, or the owner's story bank for work and career posts.
3. Draft with the chosen style and hook, voice block, structure hint and recent results.
4. Run the gates; on failure the reasons are fed back to the model (up to 3 attempts).
5. Log the AI-tell score, then run the editor (rewrite once if below the bar).
6. Publish through the API; read analytics hourly, then at 72 hours for the trial.

## Design decisions

- **Gates before models.** Cheap, deterministic checks run first, so free-tier model calls are spent only on drafts that can pass.
- **Facts are the only source of numbers.** The numbers gate makes honesty a property of the pipeline, not of the prompt.
- **Status quo wins ties.** The experiment only changes the format or slot when a result clears a permutation test and a minimum lift.
- **Append-only analytics.** Measurements are never overwritten, so any later analysis can be reproduced.
- **Quality caps.** At most 2 IPO posts a week and a minimum IPO quality score, so volume never beats quality.
