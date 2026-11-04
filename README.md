# LinkedIn Growth MCP

A self-hosted [MCP](https://modelcontextprotocol.io) server and autopilot that helps one person grow their own LinkedIn account with **useful, honest posts**: it drafts in the owner's voice, checks every post with deterministic gates, publishes through the official LinkedIn API, then measures what worked and adjusts.

It was built by a business analyst for their own account, as a practical project in data pipelines, experiment design and guarded automation. It is a personal tool, not a growth hack or a posting farm.

> **Quality over quantity.** Every claim in a post must trace to a fact the owner supplied. Numbers that are not in the fact pack block the post. Nothing is invented.

## What it does

| Area | What happens |
|---|---|
| **Drafting** | Gemini drafts in a chosen style and hook (6 styles x 4 hooks), using the owner's voice fingerprint, past results and a fact pack ("story bank"). |
| **Gates (no model calls)** | Form and safety checks: length, links, hashtags, dashes, advice wording; off-limits terms; **every number must appear in the verified facts**; a regex AI-tell lint is logged as a soft score. |
| **Editor** | A second model pass scores the draft out of 10 and rewrites once below the bar. |
| **Publishing** | Official LinkedIn API only. Posts go out on weekdays at a slot assigned by the time test. An owner can also schedule one exact draft (`schedule_publish`). |
| **Measurement** | Hourly analytics snapshots (read-only browser), 72-hour readings, audience and follower tracking, comment-quality analysis. All data is kept append-only. |
| **Learning** | A Thompson-sampling bandit shifts style and hook choice toward what earns reach. A concurrent format (short vs long) and posting-slot (9, 13, 18 IST) trial decides timing with a permutation test and a status-quo-wins rule. |
| **Topic mix** | A weekly mix across IPO analysis, tools, work stories and a career series, steered by results with a 30-day floor. A weekly idea fill proposes new, validated backlog ideas. |
| **IPO posts** | Facts are extracted from the filing (RHP), compared with the closest peer, and ranked by a 0 to 10 quality score. At most 2 IPO posts a week, best first. Education only, never buy/sell advice. |
| **Dashboard** | A local health page (port 8790) with checks, recent posts, jobs and a Veto/Restore list for backlog ideas. |

## Architecture

```
            +-------------------+        +------------------+
 MCP client |  server.py (tools)|        |  dashboard.py    |
 (Claude)-->|  FastMCP, ~60     |        |  stdlib, :8790   |
            +---------+---------+        +--------+---------+
                      |                           |
              +-------v-------+   APScheduler     | read-only
              |  service.py   |<--- hourly / cron  | (+ idea veto)
              +---+-------+---+                    |
                  |       |                 +------v------+
        +---------v-+   +-v-----------+     |  SQLite     |
        | autopilot |   | strategy/   |     | data/growth |
        | (decides) |   | gates, bandit, experiments,     |
        +-----+-----+   | topics, hooks, aitells, ...     |
              |         +-----------------+---------------+
   +----------+----------+                |
   |          |          |         +------v------+
 Gemini   LinkedIn API  Patchright  | store/db.py |
 (draft,  (publish)     (read-only  +-------------+
 editor)                 analytics)
```

More detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The editorial and safety rules live in [RULEBOOK.md](RULEBOOK.md).

## Safety and honesty rules

- **Approval-gated by default.** Publishing needs an explicit confirm, or the owner has switched the autopilot to live mode.
- **No invented numbers.** `strategy/gates.py` rejects any figure missing from the supplied facts (small integers, years, and values derivable from the facts are allowed).
- **Off-limits terms** are enforced in code, not just in the prompt.
- **Own account only.** Browser access is read-only and capped per day. Automated access can violate LinkedIn's terms and risk account restriction, so use it on your own account at your own risk.
- **No financial advice.** IPO posts are analysis for education and carry a disclaimer.
- **Nothing is deleted.** Metrics, analyses and ideas are append-only; "vetoed" ideas are only marked dropped.

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), a Gemini API key, a LinkedIn developer app (the "Share on LinkedIn" product).

```bash
git clone https://github.com/21BPS1591/linkedin-growth-mcp
cd linkedin-growth-mcp
cp .env.example .env            # fill in keys; see the comments in the file
uv sync
uv run patchright install chromium
uv run -m linkedin_growth_mcp --login            # sign in once (read-only browser)
uv run -m linkedin_growth_mcp --authorize-linkedin   # one-time posting authorization
```

Run the server (HTTP needs `MCP_AUTH_TOKEN`):

```bash
uv run -m linkedin_growth_mcp --transport http --host 127.0.0.1 --port 8000
```

On Windows, `install-autostart.ps1` registers a hidden scheduled task that starts the server at login and restarts it if it stops. `dashboard.cmd` opens the dashboard.

## Typical flow

`sync_profile` -> `save_interests` -> `save_story_bank` / `save_voice_samples` -> `propose_post` -> review -> `approve_and_publish`, or let the autopilot run. Useful tools: `autopilot_status`, `experiment_status`, `hook_report`, `reply_queue`, `profile_scorecard`, `schedule_publish`.

## Tests

```bash
uv run pytest
```

About 140 tests cover the gates, bandit, experiment statistics, topic mix, IPO ranking, scheduling and tracking.

## Project layout

```
src/linkedin_growth_mcp/
  server.py        MCP tools and scheduler jobs
  service.py       facade behind every tool
  autopilot.py     the daily decision loop
  gemini/          text and image calls, per-job key and quota guards
  linkedin/        publishing, analytics, profile, browser (read-only)
  ipo/             filing extraction, peer comparison, market data
  strategy/        gates, bandit, experiments, topics, hooks, voice, lints
  store/           SQLite schema and queries
tests/             pytest suite
dashboard.py       local health dashboard
RULEBOOK.md        editorial and safety rules
```

## License

MIT. See [LICENSE](LICENSE).
