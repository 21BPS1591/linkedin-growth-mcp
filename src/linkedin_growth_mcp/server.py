"""FastMCP app, tool registration, and CLI entrypoint."""
from __future__ import annotations

import argparse
import asyncio
import hmac
from typing import Any

from fastmcp import FastMCP

from .config import load_config
from .service import Service
from .store.db import Store

cfg = load_config()
svc = Service(cfg, Store(cfg.db_path))
from .gemini.keys import describe as _describe_keys  # noqa: E402

svc.store.set_kv("gemini_keys", _describe_keys(cfg))  # account, env var and cap per key for the dashboard; never a key
mcp = FastMCP("linkedin-growth")


@mcp.tool
async def sync_profile() -> dict[str, Any]:
    """Read your own LinkedIn profile (read-only browser) and store a snapshot."""
    return await svc.sync_profile()


@mcp.tool
def get_interest_questions() -> list[str]:
    """Questions to ask the user about their interests/goals. Ask them, then call save_interests."""
    return svc.interest_questions()


@mcp.tool
def save_interests(interests: dict[str, Any]) -> dict[str, Any]:
    """Store the user's answers (e.g. topics, audience, goal, voice, no_go, admired_creators)."""
    return svc.save_interests(interests)


@mcp.tool
def assess_fit() -> dict[str, Any]:
    """Score how well the profile matches the stated interests; returns gaps and post angles."""
    return svc.assess_fit()


@mcp.tool
def list_ideas(series: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    """The content backlog. Series: ipo, bi_features, product, story, ai. Status: backlog, drafted, published, dropped."""
    return svc.list_ideas(series, status)


@mcp.tool
def add_idea(series: str, title: str, brief: str, priority: int = 2, facts: str = "") -> dict[str, Any]:
    """Add a post idea to the backlog (priority 1=high..3=low). `facts` = the only figures the draft may state."""
    return svc.add_idea(series, title, brief, priority, facts)


@mcp.tool
def story_interview_questions() -> list[str]:
    """The 5 spine questions (Moment, Number, Correction, Opposition, Ask). Ask one at a time, keep the user's words."""
    return svc.story_interview_questions()


@mcp.tool
def save_story_interview(title: str, moment: str = "", number: str = "", correction: str = "",
                         opposition: str = "", ask: str = "", series: str = "story", priority: int = 2) -> dict[str, Any]:
    """Turn interview answers into a backlog idea; the answers also become its verified FACTS. Never invent answers."""
    return svc.save_story_interview(title, moment, number, correction, opposition, ask, series, priority)


@mcp.tool
def save_voice_samples(samples: list[str]) -> dict[str, Any]:
    """Save 3-6 posts/comments the owner TYPED themselves (not AI output); builds a voice fingerprint used in drafts."""
    return svc.save_voice_samples(samples)


@mcp.tool
def profile_scorecard() -> dict[str, Any]:
    """Rule-based profile grade from the last sync_profile text (no model call); unreadable sections say unknown."""
    return svc.profile_scorecard()


@mcp.tool
def save_story_bank(facts: str) -> dict[str, Any]:
    """Save your own verified stories and figures (mark estimates "(est.)"). Non-IPO drafts may state only these numbers."""
    return svc.save_story_bank(facts)


@mcp.tool
def update_idea(idea_id: int, facts: str | None = None, status: str | None = None,
                priority: int | None = None, brief: str | None = None) -> dict[str, Any]:
    """Edit an idea. Put verified numbers (DRHP figures, peer data, GMP with source/date) in `facts`; drafts may use only those."""
    return svc.update_idea(idea_id, facts, status, priority, brief)


@mcp.tool
def propose_post(idea_id: int | None = None) -> dict[str, Any]:
    """Draft a post (style chosen by the performance bandit), optionally from a backlog idea. Does NOT publish."""
    return svc.propose_post(idea_id)


@mcp.tool
def edit_draft(draft_id: int, text: str | None = None, image_prompt: str | None = None) -> dict[str, Any]:
    """Replace the text and/or image prompt of a pending draft."""
    return svc.edit_draft(draft_id, text, image_prompt)


@mcp.tool
def generate_image(draft_id: int) -> dict[str, Any]:
    """Generate the draft's image with Gemini and attach it to the draft."""
    return svc.generate_image(draft_id)


@mcp.tool
def ingest_rhp(filename: str) -> dict[str, Any]:
    """Extract issue details, financials, ratios, objects, promoter holding and peers (with RHP page numbers) from a PDF in the filings folder."""
    return svc.ingest_rhp(filename)


@mcp.tool
def get_ipo(ipo_id: int) -> dict[str, Any]:
    """Show the stored IPO facts and whether they are verified."""
    return svc.get_ipo(ipo_id)


@mcp.tool
def correct_ipo(ipo_id: int, corrections: dict[str, Any]) -> dict[str, Any]:
    """Fix extracted figures after checking the RHP. Nested dicts merge; lists replace. Resets verification."""
    return svc.correct_ipo(ipo_id, corrections)


@mcp.tool
def confirm_ipo(ipo_id: int) -> dict[str, Any]:
    """Mark the IPO figures as human-verified against the RHP."""
    return svc.confirm_ipo(ipo_id)


@mcp.tool
def attach_ipo_facts(ipo_id: int, idea_id: int) -> dict[str, Any]:
    """Copy VERIFIED IPO facts into a backlog idea so the draft may cite them."""
    return svc.attach_ipo_facts(ipo_id, idea_id)


@mcp.tool
def preview_next_post(refresh: bool = False) -> dict[str, Any]:
    """Show (and lock in) the next autopilot post. The next cycle publishes exactly this text; refresh=true redrafts it."""
    return svc.autopilot.preview_next(refresh)


@mcp.tool
def schedule_publish(draft_id: int, at: str) -> dict[str, Any]:
    """Publish this exact pending draft at a local date-time like 2026-11-04T18:07. Runs inside the server (no Claude session needed); text must stay unchanged."""
    return svc.schedule_publish(draft_id, at)


@mcp.tool
def reply_queue() -> dict[str, Any]:
    """Posts that need a reply now (stage watch/hot/warm/cool/move to DM by post age, unanswered count). No model call."""
    return svc.reply_queue()


@mcp.tool
def conversation_report(limit: int = 50) -> dict[str, Any]:
    """Quality of the comments you receive and of your own replies, per post, with running averages and coaching."""
    return svc.conversation_report(limit)


@mcp.tool
async def check_comments_now(post_id: int | None = None, force: bool = False) -> dict[str, Any]:
    """Read a post's page now and score its conversation (defaults to the most recent post). Replies are never sent."""
    if post_id is None:
        posts = svc.store.list_posts()
        if not posts:
            return {"error": "no published post found"}
        post_id = max(posts, key=lambda p: p["published_at"])["id"]
    return await svc.check_comments(post_id, force)


@mcp.tool
async def track_followers() -> dict[str, Any]:
    """Read and store today's follower count from your profile."""
    return await svc.track_followers()


@mcp.tool
def follower_history(limit: int = 90) -> dict[str, Any]:
    """Daily follower counts collected so far, newest first, with the change over the period."""
    return svc.follower_history(limit)


@mcp.tool
def api_keys_report() -> dict[str, Any]:
    """Which Google account owns each Gemini key, what each key is used for, today's use against its cap, and which jobs are paused for lack of a key."""
    return svc.api_keys_report()


@mcp.tool
def check_hook(text: str) -> dict[str, Any]:
    """Score the opening of a draft (first 210 characters) against the rule book and list what to fix. No AI call."""
    return svc.check_hook(text)


@mcp.tool
def hook_report() -> dict[str, Any]:
    """Which opening patterns earned reach on your published posts, with sample sizes. Honest when data is too thin."""
    return svc.hook_report()


@mcp.tool
def suggest_hooks(text: str, topic: str = "") -> dict[str, Any]:
    """Alternative opening lines for a draft, each linted. Uses one Gemini call from the daily cap; manual only."""
    return svc.suggest_hooks(text, topic)


@mcp.tool
def experiment_status() -> dict[str, Any]:
    """The format + posting-time trial (short vs long posts, 9/13/18 IST): locked until 2026-11-04, then runs by itself."""
    return svc.experiment_status()


@mcp.tool
def start_experiment(name: str = "trial") -> dict[str, Any]:
    """Start the trial now (the autopilot does this itself on 2026-11-04). Refused before that date."""
    return svc.start_experiment(name)


@mcp.tool
def finish_experiment() -> dict[str, Any]:
    """Archive the trial once both the format and the posting slot have a final verdict."""
    return svc.finish_experiment()


@mcp.tool
def posting_time_report() -> dict[str, Any]:
    """How each candidate posting hour has performed, and which hour is in use today."""
    return svc.autopilot.posting_time_report()


@mcp.tool
def feedback_report() -> dict[str, Any]:
    """What the audience's comments taught the system: active writing rules and recent per-post comment analysis."""
    return svc.feedback_report()


@mcp.tool
def retire_feedback_rule(rule_id: int) -> dict[str, Any]:
    """Switch off a learned writing rule (e.g. one that was wrongly learned)."""
    svc.store.retire_rule(rule_id)
    return {"retired": rule_id}


@mcp.tool
async def snapshot_analytics_now(force: bool = False) -> dict[str, Any]:
    """Read every young post's live analytics and audience now and append them to the history (the autopilot also does this hourly)."""
    return await svc.snapshot_analytics(force=force)


@mcp.tool
def autopilot_status() -> dict[str, Any]:
    """Autonomy mode, LinkedIn posting token health, whether the saved LinkedIn browser login still works (linkedin_browser_session), tracked IPOs and the recent decision log."""
    return svc.autopilot.status()


@mcp.tool
def set_autonomy(mode: str) -> dict[str, Any]:
    """off | shadow (run everything, publish nothing) | live (publish posts that pass every gate)."""
    return svc.autopilot.set_mode(mode)


@mcp.tool
def run_autopilot_now() -> dict[str, Any]:
    """Run one autopilot cycle immediately (normally runs hourly)."""
    return svc.autopilot.tick()


@mcp.tool
def attach_image(draft_id: int, filename: str) -> dict[str, Any]:
    """Attach your own image (a .png/.jpg you placed in the images folder) to a pending draft."""
    return svc.attach_image(draft_id, filename)


@mcp.tool
def review_draft(draft_id: int) -> dict[str, Any]:
    """Gemini critique of the draft text and its attached image: score, hook, weaknesses, rewrite, publish-readiness."""
    return svc.review_draft(draft_id)


@mcp.tool
def approve_and_publish(draft_id: int, confirm: bool = False) -> dict[str, Any]:
    """Publish a draft to LinkedIn. Only call after the user has explicitly approved this exact draft; set confirm=true."""
    return svc.approve_and_publish(draft_id, confirm)


@mcp.tool
def linkedin_auth_url() -> dict[str, str]:
    """Step 1 of one-time LinkedIn OAuth for publishing."""
    return svc.linkedin_auth_url()


@mcp.tool
def linkedin_auth_complete(code: str) -> dict[str, Any]:
    """Step 2: exchange the `code` from the redirect URL for a token."""
    return svc.linkedin_auth_complete(code)


@mcp.tool
async def refresh_metrics() -> dict[str, Any]:
    """Collect analytics for published posts at the 24h/72h/7d checkpoints."""
    return await svc.refresh_metrics()


@mcp.tool
async def get_analytics_now(post_id: int | None = None) -> dict[str, Any]:
    """Fetch the latest live analytics (impressions, reactions, comments, reposts) for a post on demand; defaults to the most recent post. Read-only."""
    return await svc.analytics_now(post_id)


@mcp.tool
def record_metrics(post_id: int, checkpoint: str, impressions: int, reactions: int = 0, comments: int = 0, reposts: int = 0) -> dict[str, Any]:
    """Manually enter metrics (checkpoint: 24h, 72h or 7d) if scraping fails."""
    return svc.record_metrics(post_id, checkpoint, impressions, reactions, comments, reposts)


@mcp.tool
def import_metrics_csv(content: str) -> dict[str, Any]:
    """Import LinkedIn's content-analytics CSV export (paste its text)."""
    return svc.import_metrics_csv(content)


@mcp.tool
def import_metrics_file(path: str, checkpoint: str = "7d", dry_run: bool = False) -> dict[str, Any]:
    """Import LinkedIn's analytics export from a file on disk (.xlsx or .csv); rows are matched to our posts by post id in the URL."""
    return svc.import_metrics_file(path, checkpoint, dry_run)


@mcp.tool
def import_audience_file(path: str) -> dict[str, Any]:
    """Save audience demographics (location, seniority, industry, company size) from a LinkedIn single-post export (.xlsx); also refreshes data/audience_demographics.csv for analysis."""
    return svc.import_audience_file(path)


@mcp.tool
def audience_report(post_id: int | None = None) -> dict[str, Any]:
    """Stored audience demographics per post and snapshot."""
    return svc.audience_report(post_id)


@mcp.tool
def export_ipo_metrics() -> dict[str, Any]:
    """Write data/ipo_metrics.csv (one row per IPO: issue, latest financials, margins, CAGR, P/E, peers, linked post analytics) and data/ipo_financials.csv (one row per IPO per fiscal year)."""
    return svc.export_ipo_metrics()


@mcp.tool
def sync_market_data(max_pages: int = 40) -> dict[str, Any]:
    """Pull the chittorgarh.com IPO dashboard and every listed IPO's details, peers, subscription and anchor letter into the database (no Gemini). Runs automatically; call to refresh now."""
    return svc.sync_market(max_pages)


@mcp.tool
def weekly_digest() -> dict[str, Any]:
    """Write and return the weekly digest (posts and their analytics, audience, IPO pipeline, listing outcomes, system health). Also written automatically each week."""
    return svc.weekly_digest()


@mcp.tool
def sync_exchange_listings() -> dict[str, Any]:
    """Fetch NSE and BSE listing-day prices (open, high, low, close, previous close) from the exchanges' own end-of-day files for every IPO with a confirmed listing day. Primary source; cached and never deleted."""
    return svc.sync_exchange_listings()


@mcp.tool
def analyze_performance() -> dict[str, Any]:
    """Per-post scores and a plain-language summary of what is working."""
    return svc.analyze_performance()


@mcp.tool
def next_strategy() -> dict[str, Any]:
    """Current best-performing style/hook arms and what the next post will lean toward."""
    return svc.next_strategy()


class BearerAuth:
    """ASGI middleware: reject requests without the shared bearer token."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, b"Bearer " + self.token):
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


async def _serve_http(host: str, port: int) -> None:
    import uvicorn
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    if not cfg.mcp_auth_token:
        raise SystemExit("MCP_AUTH_TOKEN must be set to run the HTTP transport")
    sched = AsyncIOScheduler()
    sched.add_job(svc.refresh_metrics, "interval", hours=6, id="refresh_metrics")

    async def _tick():
        await asyncio.to_thread(svc.autopilot.tick)

    from datetime import datetime, timedelta

    # a cycle shortly after start-up (covers a late PC wake), hourly, and exactly at the daily posting hour
    sched.add_job(_tick, "interval", minutes=60, id="autopilot", max_instances=1, coalesce=True,
                  next_run_time=datetime.now() + timedelta(seconds=45))
    sched.add_job(_tick, "cron", hour=cfg.post_hour, minute=0, id="autopilot_daily", max_instances=1, coalesce=True)
    lead = cfg.post_hour * 60 - 30  # the draft window opens 30 minutes before the posting hour (autopilot.DRAFT_LEAD_MINUTES)
    if lead >= 0:
        sched.add_job(_tick, "cron", hour=lead // 60, minute=lead % 60, id="autopilot_lead", max_instances=1, coalesce=True)
    async def _scheduled():
        await asyncio.to_thread(svc.autopilot.publish_scheduled)

    sched.add_job(_scheduled, "interval", minutes=2, id="scheduled_publish", max_instances=1, coalesce=True)
    sched.start()
    app = BearerAuth(mcp.http_app(path="/mcp"), cfg.mcp_auth_token)
    await uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info")).serve()


def main() -> None:
    import sys

    if sys.stdout is None or sys.stderr is None:  # started with pythonw.exe: no console, so log to a file instead
        log = open(cfg.data_dir / "server.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    ap = argparse.ArgumentParser(prog="linkedin-growth-mcp")
    ap.add_argument("--login", action="store_true", help="open a browser to sign in to LinkedIn once")
    ap.add_argument("--authorize-linkedin", action="store_true", help="one-time sign-in that allows posting")
    ap.add_argument("--export-session", action="store_true", help="write LinkedIn cookies to a portable file for Docker")
    ap.add_argument("--refresh-metrics", action="store_true", help="force refresh analytics for all tracked posts now")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    if args.login:
        ok = asyncio.run(svc.browser.login_interactive())
        raise SystemExit(0 if ok else "Login did not complete in time")
    if args.authorize_linkedin:
        from .linkedin import oauth

        print(oauth.authorize_interactive(cfg))
        return
    if args.export_session:
        n = asyncio.run(svc.browser.export_session())
        print(f"Exported {n} cookies to {cfg.session_path}")
        return
    if args.refresh_metrics:
        import time as _t
        print("Reading live analytics for all tracked posts from LinkedIn...")
        res = asyncio.run(svc.snapshot_analytics(force=True))
        n_saved = len(res.get("saved", []))
        n_failed = len(res.get("failed", []))
        svc.store.set_kv("last_analytics_snapshot", _t.time())
        svc.store.add_log("analytics_snapshot", f"{n_saved} posts saved, {n_failed} failed (manual refresh via script)")
        print(f"Done! {n_saved} post(s) updated, {n_failed} failed.")
        return
    if args.transport == "http":
        asyncio.run(_serve_http(args.host, args.port))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
