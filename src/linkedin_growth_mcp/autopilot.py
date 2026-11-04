"""Unattended pipeline: find new IPO RHPs, verify them, and post the pre-IPO series when every gate passes.

Modes (stored in the DB, overridable by creating the file `<data dir>/STOP`):
  off     do nothing
  shadow  run everything, save passing drafts as 'shadow_pass', publish NOTHING   (default)
  live    publish drafts that pass every deterministic gate
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

from pypdf import PdfReader

from .ipo import enrich, image_prompt, rhp, sebi
from .ipo.discovery import download_rhp, parse_date, slug
from .strategy import aitells, gates, topics
from .strategy.bandit import StyleBandit
from .strategy.polish import polish
from .strategy.scoring import post_score
from .strategy.timing import SlotBandit
from .strategy.styles import arm_key

MODES = ("off", "shadow", "live")
MAX_INGEST_PER_TICK = 2
SYNC_EVERY_HOURS = 6
MAX_BLOCKED_ATTEMPTS = 3
EDITOR_MAX_ROUNDS = 2  # one drafting round, then at most one rewrite guided by the editor
DRAFT_LEAD_MINUTES = 30  # the day's IPO post is drafted and checked this long before the posting hour, so a failed draft can retry in time
DRAFT_EXPIRY_HOURS = 6  # an unapproved draft is deleted this long after it was made, once the day already has its post
TOPIC_ROLLOUT = ("product", "ai")  # topics unlocked one at a time, in this order, once the current set has proven itself
UNLOCK_MIN_POSTS = 5  # measured posts of the current set needed before the next topic is added
UNLOCK_FALLBACK_BAR = 0.5  # used only until there are measured posts from before the current test to set the bar
UNLOCK_BAR_RANGE = (0.4, 0.7)  # the learned bar is kept inside this range so one outlier cannot freeze or open the rollout
DONE = ("shadow_pass", "published")

BRIEFS = {
    "compare": (
        "ONE reference post on ONE IPO that a serious reader can bookmark and use instead of reading the RHP: the issue, "
        "who is selling and who bought, the numbers, the valuation against the single closest peer, where the money goes, "
        "and what to watch. Use ONLY numbers and statements in FACTS. Neutral tone: facts and trade-offs, no recommendation, "
        "no ranking, no 'apply/avoid'. End with 'Educational analysis, not investment advice.'\n"
        "STRUCTURE (follow in order; SKIP any part whose facts are not in FACTS, never fill a gap): "
        "(a) HOOK, 1-2 sentences: plain English on what the company does (paraphrase ONLY the 'Business' line in FACTS, never infer it "
        "from the name) and the one thing that makes this IPO worth reading. "
        "(b) THE ISSUE: open and close dates, price band, lot size and minimum bid, total size split into fresh issue and offer for sale "
        "(say the offer-for-sale money goes to the selling shareholders, not the company). "
        "(c) WHO IS SELLING AND WHO BOUGHT: the promoter holding before the issue, the split of the offer for sale between promoters and "
        "other shareholders, the pre-IPO round and its price versus the band, and the anchor round (total, number of investors, the largest "
        "names, the category mix). Use only what FACTS contain. "
        "(d) THE NUMBERS: revenue and profit over the three years with the CAGR, the latest EBITDA and profit margin, borrowings and "
        "net worth if given. "
        "(e) VALUATION: the P/E at the upper band and the return ratios for the company versus the ONE closest peer by revenue "
        "(say it is the closest by revenue and not the same size, and that ratio definitions can differ), using the 'Computed:' lines exactly. "
        "(f) WHERE THE MONEY GOES: each 'Object:' line with its amount. "
        "(g) WHAT TO WATCH: exactly two short bullets, each tied to a fact in FACTS (for example the share of the issue that is offer for sale, "
        "borrowings, a valuation gap, or the pre-IPO price versus the band). Describe the trade-off; do not advise. "
        "(h) ONE specific question about the business, then the disclaimer line.\n"
        "STYLE: 1,600 to 2,300 characters in total. The first two lines must hold the hook because feeds cut the text after about 210 characters. "
        "Scannable: short paragraphs, plain-text labels such as 'The issue:' or 'Who is selling:' at the start of a line, and '-' bullets for "
        "lists. No emojis. Round rupee amounts to whole crore (write '₹804 crore'; keep one decimal under ₹100 crore), percentages to one decimal, "
        "never three decimals. Show growth as a percentage or CAGR computed from FACTS. Name at most three anchor investors.\n"
        "HARD RULES: (1) the FIRST sentence must be plain English about what the company does or why its numbers matter - never start with a "
        "number or currency. (2) Write amounts as '₹804 crore', never '803.629 Cr'. (3) Do NOT say what the IPO money will be used for unless "
        "FACTS contain an 'Object:' line. (4) Include one sentence saying what the trend MEANS, not just what it is. "
        "(5a) GRAMMAR: write every sentence so it reads correctly aloud. One idea per sentence, at most 25 words, no run-ons or comma splices, "
        "no dangling phrases. Peer comparison pattern: 'The closest peer by revenue is Ion Exchange, about 3.6x larger. Its P/E is 31.3x versus "
        "20.0x for SFC.' Re-read each sentence before answering and fix any that does not parse. "
        "(5) NEVER use em dashes or en dashes; use commas, full stops or a hyphen. (5b) At most 3 hashtags (more are added at publish), all "
        "specific (e.g. #IPO #FinancialAnalysis); no #Investing. (6) NEVER write [TBD] or any placeholder. If a detail is not in FACTS, "
        "do not mention it at all. (7) Mention no other peer than the one chosen. (8) Write investor and fund names in normal capitalisation "
        "(for example 'SBI Infrastructure Fund'), not in capitals."
    ),
}
BRIEFS["weekly"] = (
    "ONE weekend scorecard post: how the IPOs that listed this week performed on listing day against their issue price. It must be "
    "useful to someone who tracks IPOs and worth saving. Use ONLY numbers and statements in FACTS. Neutral tone: facts and trade-offs, "
    "no recommendation, no ranking of what to buy, no 'apply/avoid'. End with 'Educational analysis, not investment advice.'\n"
    "STRUCTURE (follow in order): (a) HOOK, 1-2 sentences in plain English: the headline of the week, for example how many IPOs opened above "
    "their issue price and the average gain. (b) THE SCOREBOARD: one short line per IPO from the 'Listing:' lines, best first, in the form "
    "'Company: issue price Rs X, opened at Rs Y (+Z%), closed +W%'. Include every IPO in FACTS. (c) THE PATTERN: the best and the weakest "
    "listing with their figures, and how many gave back part of the opening gain by the close. (d) WHAT IT MEANS: two sentences. Day-one "
    "moves are a snapshot and do not say how the business performs later. Add one short line that these gains apply only to investors who were "
    "allotted shares at the issue price, because oversubscribed IPOs allot retail by lottery. (e) ONE specific question to the audience, then the disclaimer line.\n"
    "STYLE: 1,400 to 2,300 characters. The first two lines must carry the hook because feeds cut the text after about 210 characters. "
    "Scannable: one line per IPO, plain-text labels such as 'The scoreboard:', no emojis. Percentages with one decimal and a plus or minus sign. "
    "Write company names in normal capitalisation.\n"
    "HARD RULES: (1) the FIRST sentence must be plain English and must not start with a number or currency. (2) Use only figures from FACTS. "
    "(3) NEVER use em dashes or en dashes; use commas, full stops or a hyphen. (4) GRAMMAR: one idea per sentence, at most 25 words, no run-ons. "
    "(5) At most 3 hashtags (more are added at publish), all specific (e.g. #IPO #ListingGains). (6) NEVER write [TBD] or any placeholder. "
    "(7) No links and no @mentions."
)
WEEKLY_MIN_IPOS = 3  # a scorecard of fewer listings is not worth a post
WEEKLY_SCORECARD_ENABLED = False  # owner decision 2026-10-31: never post a weekly scorecard
STEP = "compare"  # one post per IPO
# Arms the learning loop may pick from for IPO posts (styles that suit a data-led breakdown x opening types)
IPO_ARMS = {arm_key(s, h) for s in ("case_study", "listicle", "question", "contrarian")
            for h in ("bold_claim", "number", "question")}

_CLOSE_RX = re.compile(r"bid\s*/\s*offer\s+closes?\s+on\s*[:\-]?\s*([^\n]{0,60})", re.IGNORECASE)
_OPEN_RX = re.compile(r"bid\s*/\s*offer\s+opens?\s+on\s*[:\-]?\s*([^\n]{0,60})", re.IGNORECASE)


def cover_dates(page_texts: list[str]) -> tuple[date | None, date | None]:
    """Cheap pre-check of the RHP cover so closed IPOs are not sent to Gemini."""
    text = " ".join(" ".join(t.split()) for t in page_texts)
    text = re.sub(r"(?i)bid\s*/\s*offer\s+(opens|closes)", lambda m: "\n" + m.group(0), text)
    o = _OPEN_RX.search(text)
    c = _CLOSE_RX.search(text)
    return (parse_date(o.group(1)) if o else None, parse_date(c.group(1)) if c else None)


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_FILING_MONTH = re.compile(r"/public-issues/([a-z]{3})-(\d{4})/")


def _filing_too_old(filing_url: str, today: date) -> bool:
    """SEBI paths contain the filing month; RHPs from before the previous 3 weeks cannot be open IPOs."""
    m = _FILING_MONTH.search(filing_url)
    if not m or m.group(1) not in _MONTHS:
        return False
    cutoff = today - timedelta(days=21)
    return (int(m.group(2)), _MONTHS[m.group(1)]) < (cutoff.year, cutoff.month)


def _pid_alive(pid: int) -> bool:
    """True if a process with this id is running. (os.kill(pid, 0) would terminate it on Windows, so it is not used there.)"""
    import os

    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _revenue(data: dict[str, Any]) -> float:
    vals = [f.get("revenue_cr") for f in data.get("financials") or [] if isinstance(f.get("revenue_cr"), (int, float))]
    return max(vals) if vals else 0.0


def ipo_quality(data: dict[str, Any]) -> float:
    """0-10 score of how good an IPO is to write about: scale, a usable peer, a growth trend, a complete issue structure.
    Pure function of the verified filing data, so the choice of 'the best IPO' is explainable and repeatable."""
    import math

    score = 0.0
    rev = _revenue(data)
    if rev > 0:
        score += min(3.0, max(0.0, math.log10(rev) - 1.0))  # 100 cr -> 1, 1,000 cr -> 2, 10,000 cr -> 3
    if rhp.closest_peer(data):
        score += 2.5  # the side-by-side comparison is the core of the post
    years = [f for f in data.get("financials") or [] if isinstance(f.get("revenue_cr"), (int, float))]
    score += 2.0 if len(years) >= 3 else 1.0 if len(years) == 2 else 0.0  # growth needs 2+ years
    iss = data.get("issue") or {}
    score += 1.0 if iss.get("price_band_high") else 0.0
    score += 1.0 if iss.get("total_issue_cr") or iss.get("fresh_issue_cr") else 0.0
    score += 0.5 if data.get("business") or data.get("business_line") else 0.0
    return round(min(score, 10.0), 1)


class Autopilot:
    def __init__(self, svc: Any):
        self.svc = svc
        self.store = svc.store
        self.cfg = svc.cfg

    # ---- mode / kill switch
    def mode(self) -> str:
        if (self.cfg.data_dir / "STOP").exists():
            return "off"
        m = self.store.get_kv("autonomy", "shadow")
        return m if m in MODES else "shadow"

    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in MODES:
            raise RuntimeError(f"mode must be one of {MODES}")
        self.store.set_kv("autonomy", mode)
        self.store.add_log("mode_change", f"mode set to {mode}")
        return {"mode": self.mode(), "note": "STOP file present, forcing off" if self.mode() != mode else "ok"}

    # ---- stage 1: discover + verify RHPs
    def sync_filings(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        if not self._key_ready("extraction"):  # no key of its own: do not even download filings that cannot be read
            return {"ingested": [], "skipped_closed": [], "failed": [], "paused": "extraction key missing"}
        seen: dict[str, Any] = self.store.get_kv("seen_filings", {})
        have = {r["source_file"] for r in self.store.list_ipos()}
        rows = [r for r in sebi.latest_rhps()
                if r["filing_url"] not in seen and f"{slug(r['company'])}-rhp.pdf" not in have]
        ingested, skipped, failed = [], [], []
        attempts = 0
        for row in rows:
            if _filing_too_old(row["filing_url"], today):
                seen[row["filing_url"]] = "old"
                skipped.append(row["company"])
                continue
            if self.store.usage_today(self.cfg.usage_kind("extraction")) >= self.cfg.cap_for("extraction"):
                break  # the extraction key's own daily cap; these filings are retried tomorrow
            if attempts >= MAX_INGEST_PER_TICK:  # attempts, not successes: failures must not cause an endless loop
                break
            attempts += 1
            try:
                pdf_url = sebi.resolve_pdf(row["filing_url"])
                path = download_rhp(pdf_url, row["company"], self.cfg.filings_dir)
                opens, closes = cover_dates(rhp.page_texts(path, [0, 1, 2, 3]))
                if closes and closes < today - timedelta(days=1):
                    seen[row["filing_url"]] = "closed"
                    skipped.append(row["company"])
                    path.unlink(missing_ok=True)
                    continue
                res = self.svc.ingest_rhp(path.name)
                seen[row["filing_url"]] = f"ipo:{res['ipo_id']}"
                self.store.set_kv("seen_filings", seen)  # persist per row so an interrupted cycle never repeats work
                ingested.append(row["company"])
                self.store.add_log("ingested", f"{row['company']} verified={res['verified']} rate={res['auto_check']['match_rate']}",
                                   ipo_id=res["ipo_id"])
            except Exception as e:
                failed.append({"company": row["company"], "error": str(e)[:200]})
                if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e)[:40]:
                    self.store.add_log("quota", "Gemini quota exhausted; stopping this cycle")
                    break  # not the filing's fault: retry next cycle without counting a failure
                tries = self.store.get_kv("filing_failures", {})
                tries[row["filing_url"]] = tries.get(row["filing_url"], 0) + 1
                if tries[row["filing_url"]] >= 3:
                    seen[row["filing_url"]] = "gave_up"
                self.store.set_kv("filing_failures", tries)
                self.store.add_log("ingest_error", f"{row['company']}: {e}"[:500])
        self.store.set_kv("seen_filings", seen)
        self.store.set_kv("last_sync", time.time())
        return {"ingested": ingested, "skipped_closed": skipped, "failed": failed}

    # ---- stage 2: decide what to post
    def _active(self, today: date) -> list[tuple[Any, dict[str, Any], date, date]]:
        out = []
        for row in self.store.list_ipos():
            if not row["verified"]:
                continue
            data = json.loads(row["data"])
            iss = data.get("issue") or {}
            o, c = parse_date(iss.get("open_date")), parse_date(iss.get("close_date"))
            if o and c and o - timedelta(days=2) <= today <= c:
                out.append((row, data, o, c))
        return out

    def posted_on(self, day: date) -> bool:
        """True if a post (shadow or live) was already produced on this calendar day: one IPO post per day."""
        start = datetime(day.year, day.month, day.day).timestamp()
        marks = ",".join("?" * len(self._done()))
        row = self.store.conn.execute(
            f"SELECT 1 FROM autopilot_log WHERE action IN ({marks}) AND ts>=? AND ts<? LIMIT 1",
            (*self._done(), start, start + 86400),
        ).fetchone()
        return row is not None

    def _done(self) -> tuple[str, ...]:
        """What counts as 'already posted': in live mode only real publications (shadow runs are rehearsals)."""
        return ("published",) if self.mode() == "live" else DONE

    def next_candidate(self, today: date | None = None) -> tuple[Any, dict[str, Any], str] | None:
        """All open IPOs are queued, closing soonest first (largest revenue breaks ties); one gets posted per day."""
        today = today or date.today()
        if self.posted_on(today) or not self.is_post_day(today):
            return None
        week_start = time.time() - 7 * 86400
        marks = ",".join("?" * len(self._done()))
        recent = self.store.conn.execute(
            f"SELECT COUNT(*) c FROM autopilot_log WHERE action IN ({marks}) AND ts>=?", (*self._done(), week_start)).fetchone()["c"]
        if recent >= self.cfg.ipo_max_per_week:
            return None  # quality over quantity: the cap for this week is used, the day goes to a non-IPO post
        # only IPOs good enough to write about; the best one first (closing soonest breaks ties)
        queue = sorted(((ipo_quality(t[1]), t) for t in self._active(today)),
                       key=lambda qt: (-qt[0], qt[1][3], -_revenue(qt[1][1])))
        for quality, (row, data, o, c) in queue:
            if quality < self.cfg.ipo_min_quality:
                continue
            if not self.store.step_logged(row["id"], STEP, self._done()):
                return row, data, STEP
        return None

    # ---- stage 3: draft, gate, (shadow|publish)
    def _idea(self, row: Any, data: dict[str, Any], step: str) -> int:
        title = f"{row['company']} - IPO {step}"
        try:
            self.svc._fill_issue_from_market(data)  # price band / lot size from synced market data, only where the RHP left them blank
        except Exception:
            pass  # no market data: write with what the filing gave
        peer = rhp.closest_peer(data)
        focused = {**data, "peers": [peer] if peer else []}  # the draft only ever sees the one chosen peer
        try:
            market_lines = self.svc.market_fact_lines(row["company"], row["id"], (data.get("issue") or {}).get("price_band_high"))  # lot, objects, anchor round, sellers: only what is present
        except Exception:
            market_lines = []
        facts = "\n".join([rhp.format_facts(focused), *enrich.comparison_lines(data, peer), *market_lines])
        existing = next((i for i in self.store.list_ideas("ipo") if i["title"] == title), None)
        if existing:
            self.store.update_idea(existing["id"], facts=facts)
            return existing["id"]
        return self.store.add_idea("ipo", title, BRIEFS[step], priority=1, facts=facts)

    def run_step(self, row: Any, data: dict[str, Any], step: str) -> dict[str, Any]:
        mode = self.mode()
        day_start = time.mktime(date.today().timetuple())
        if self.store.step_logged(row["id"], step, ("blocked", "error"), since_ts=day_start):
            return {"action": "waiting", "why": "blocked or failed earlier today; retrying tomorrow"}
        if self.posted_on(date.today()):
            return {"action": "deferred", "why": "already posted today; next IPO tomorrow"}
        data = self.svc.enrich_ipo(row["id"])  # sourced business line, issue structure, price band
        idea_id = self._idea(row, data, step)
        facts = self.store.get_idea(idea_id)["facts"]
        draft = self._queued_draft(idea_id, facts)  # a post previewed earlier is published exactly as previewed
        if draft is None:
            draft, reasons, last_id = self._draft_passing(idea_id, facts)
            if draft is None:
                self.store.add_log("blocked", "; ".join(reasons), ipo_id=row["id"], step=step, draft_id=last_id)
                return {"action": "blocked", "reasons": reasons}
        did = draft["draft_id"]
        self._attach_calendar_image(did, row, data)
        if mode != "live":
            self.store.update_draft(did, status="shadow_pass")
            self.store.add_log("shadow_pass", f"{row['company']} {step}: all gates passed (not published: mode={mode})",
                               ipo_id=row["id"], step=step, draft_id=did)
            return {"action": "shadow_pass", "draft_id": did}
        try:
            res = self.svc.approve_and_publish(did, True)
        except Exception as e:
            msg = str(e)
            action = "needs_reauth" if ("token" in msg.lower() or "authorized" in msg.lower()) else "error"
            self.store.add_log(action, msg, ipo_id=row["id"], step=step, draft_id=did)
            return {"action": action, "error": msg}
        self.store.add_log("published", res.get("url") or "", ipo_id=row["id"], step=step, draft_id=did)
        return {"action": "published", **res}

    def _log_ai_tells(self, cand: dict[str, Any]) -> None:
        """Soft AI-tell score (regex, no model call). Logged for review, never blocks a post."""
        try:
            res = aitells.lint_ai_tells(cand["text"])
            self.store.add_log("ai_tells", f"draft {cand['draft_id']}: score {res['score']}, flags: "
                               + (", ".join(res["flags"]) or "none"), draft_id=cand["draft_id"])
        except Exception:
            pass

    def _draft_passing(self, idea_id: int, facts: str) -> tuple[dict[str, Any] | None, list[str], int | None]:
        """Draft up to MAX_BLOCKED_ATTEMPTS times until a draft passes every gate. Returns (draft|None, reasons, last id)."""
        reasons: list[str] = []
        last_id = None
        best: tuple[float, dict[str, Any], int] | None = None  # the highest-scoring draft that passed every gate
        for _ in range(MAX_BLOCKED_ATTEMPTS):
            draft = self.svc.propose_post(idea_id, arms=IPO_ARMS, hints=reasons or None)  # tell the model what failed
            last_id = draft["draft_id"]
            tidy = polish(draft["text"])
            if tidy != draft["text"]:
                self.store.update_draft(last_id, text=tidy)
                draft["text"] = tidy
            reasons = gates.check_post(draft["text"], facts, "ipo", self.store.get_kv("banned_terms", []) or [])
            if not reasons:
                self._log_ai_tells(draft)
                keep, score, reasons = self._editor_round(draft, best)
                if keep is not None:
                    return keep, [], keep["draft_id"]
                best = (score, draft, last_id) if best is None or score > best[0] else best
                continue
            self.store.update_draft(last_id, status="blocked")
        if best is not None:  # the editor was never satisfied: publish the best draft rather than nothing
            self.store.add_log("editor_best_effort", f"draft {best[2]} published at editor score {best[0]:.1f}", draft_id=best[2])
            self.store.update_draft(best[2], status="pending_approval")
            return best[1], [], best[2]
        return None, reasons, last_id

    def _editor_round(self, draft: dict[str, Any], best: Any) -> tuple[dict[str, Any] | None, float, list[str]]:
        """Score a gate-passing draft. Returns (draft to publish or None, score, rewrite hints)."""
        if not self.cfg.editor_gate:
            return draft, 10.0, []
        score, weak = self._editor_score(draft["draft_id"])
        if score >= self.cfg.editor_min_score:
            return draft, score, []
        if best is not None:  # already had one rewrite round: stop spending requests on this post
            return None, score, []
        self.store.add_log("editor_rewrite", f"draft {draft['draft_id']} scored {score:.1f} of 10; rewriting once",
                           draft_id=draft["draft_id"])
        self.store.update_draft(draft["draft_id"], status="blocked")
        return None, score, weak or ["the editor rated this draft weak: sharpen the hook and cut filler"]

    def _queued_draft(self, idea_id: int, facts: str) -> dict[str, Any] | None:
        """A previewed draft waiting for its slot, if it still passes every gate against the current facts."""
        did = self.store.get_idea(idea_id)["draft_id"]
        d = self.store.get_draft(did) if did else None
        if d is None or d["status"] != "queued":
            return None
        if gates.check_post(d["text"], facts, "ipo", self.store.get_kv("banned_terms", []) or []):
            self.store.update_draft(did, status="blocked")
            return None
        self.store.update_draft(did, status="pending_approval")  # the publish path requires this status
        return {"draft_id": did, "text": d["text"]}

    def preview_next(self, refresh: bool = False) -> dict[str, Any]:
        """Draft the next post now, run every gate, and queue it: the next cycle publishes exactly this text."""
        now = datetime.now()
        day = date.today()
        if self.posted_on(day) or now.hour >= self.post_hour():
            day = day + timedelta(days=1)  # today's slot is used up or past
        cand = self.next_candidate(day)
        if not cand:
            return {"queued": False, "why": "no open IPO needs a post in that slot",
                    "mode": self.mode()}
        row, data, step = cand
        data = self.svc.enrich_ipo(row["id"])
        idea_id = self._idea(row, data, step)
        facts = self.store.get_idea(idea_id)["facts"]
        did = self.store.get_idea(idea_id)["draft_id"]
        d = self.store.get_draft(did) if did else None
        if d is not None and d["status"] == "queued" and not refresh and not gates.check_post(d["text"], facts, "ipo", self.store.get_kv("banned_terms", []) or []):
            draft = {"draft_id": did, "text": d["text"]}
        else:
            if d is not None and d["status"] == "queued":
                self.store.update_draft(did, status="superseded")
            draft, reasons, _ = self._draft_passing(idea_id, facts)
            if draft is None:
                return {"queued": False, "why": "every draft attempt failed the safety checks", "reasons": reasons}
            self.store.update_draft(draft["draft_id"], status="queued")
            self.store.add_log("preview", f"{row['company']} {step}: draft {draft['draft_id']} queued for {day} {self.post_hour()}:00",
                               ipo_id=row["id"], step=step, draft_id=draft["draft_id"])
        meta = self.store.get_draft(draft["draft_id"])
        return {"queued": True, "company": row["company"], "goes_live": f"{day} around {self.post_hour()}:00 (mode={self.mode()})",
                "draft_id": draft["draft_id"], "style": meta["style"], "hook": meta["hook"], "chars": len(draft["text"]),
                "safety_checks": "all passed", "text": draft["text"]}

    def _dates(self, data: dict[str, Any]) -> tuple[date, date] | None:
        iss = data.get("issue") or {}
        o, c = parse_date(iss.get("open_date")), parse_date(iss.get("close_date"))
        return (o, c) if o and c else None

    def _attach_calendar_image(self, draft_id: int, row: Any, data: dict[str, Any]) -> None:
        """Give the draft the calendar image prompt; attach the user's own image if they have saved one."""
        dates = self._dates(data)
        if not dates or not self.cfg.attach_images:
            return  # text-only posts by default
        self.store.update_draft(draft_id, image_prompt=image_prompt.calendar_prompt(row["company"], *dates))
        name = image_prompt.image_filename(row["company"])
        for ext in (".png", ".jpg", ".jpeg"):
            f = self.cfg.images_dir / (name[:-4] + ext)
            if f.is_file() and f.stat().st_size <= 8 * 1024 * 1024:
                self.store.update_draft(draft_id, image_path=str(f))
                break

    def image_requests(self, today: date | None = None) -> list[dict[str, Any]]:
        """For each IPO currently in its posting window: the prompt to paste into an image tool and the file name to use."""
        today = today or date.today()
        out = []
        if not self.cfg.attach_images:
            return out
        for row, data, o, c in sorted(self._active(today), key=lambda t: (t[3], -_revenue(t[1]))):
            name = image_prompt.image_filename(row["company"])
            have = any((self.cfg.images_dir / (name[:-4] + e)).is_file() for e in (".png", ".jpg", ".jpeg"))
            out.append({"company": row["company"], "opens": str(o), "closes": str(c), "save_image_as": name,
                        "image_ready": have, "prompt": image_prompt.calendar_prompt(row["company"], o, c)})
        return out

    # ---- results loop: collect numbers the day after posting, score them, log the decision
    def token_ready(self) -> bool:
        from .linkedin import oauth

        try:
            oauth.load_token(self.cfg)
            return True
        except Exception:
            return False

    def _learn_from_comments(self) -> None:
        """At 24h and 72h after posting, read the comments and turn them into rules for future drafts."""
        import asyncio

        if not self._key_ready("insights"):
            return

        for cp, hours in (("24h", 24), ("72h", 72)):
            for p in self.store.list_posts():
                if not p["url"] or time.time() < p["published_at"] + hours * 3600 or self.store.has_post_feedback(p["id"], cp):
                    continue
                tag = f"post {p['id']} @{cp}:"
                fails = self.store.conn.execute(
                    "SELECT COUNT(*) c FROM autopilot_log WHERE action='feedback_error' AND detail LIKE ?", (tag + "%",)
                ).fetchone()["c"]
                if fails >= 3:  # give up after three failures instead of retrying every hour
                    continue
                try:
                    asyncio.run(self.svc.analyze_comments(p["id"], cp))
                except Exception as e:
                    self.store.add_log("feedback_error", f"{tag} {e}"[:300])

    def review_results(self) -> list[str]:
        """For every post whose 24h numbers are in and not yet reviewed: score it and record what changes next."""
        import asyncio

        decisions: list[str] = []
        try:
            if self.svc.due_checkpoints():
                res = asyncio.run(self.svc.refresh_metrics())
                if res.get("failed"):  # a silent failure would starve the learning loop of its data
                    first = res["failed"][0]["error"][:150]
                    self.store.add_log("metrics_failed", f"{len(res['failed'])} analytics pulls failed: {first}")
                    self._alert("analytics", "LinkedIn analytics could not be read",
                                f"{len(res['failed'])} pull(s) failed. If the browser login expired, run: uv run -m linkedin_growth_mcp --login")
        except Exception as e:
            self.store.add_log("metrics_error", str(e)[:300])
        self._learn_from_comments()
        rows = self.store.conn.execute(
            "SELECT p.id pid, d.style, d.hook, m.impressions, m.reactions, m.comments, m.reposts "
            "FROM metrics m JOIN posts p ON p.id=m.post_id JOIN drafts d ON d.id=p.draft_id WHERE m.checkpoint='24h'"
        ).fetchall()
        _, scored, baseline = self.svc._history_scores()
        scores = [s for _, _, s in scored]
        for r in rows:
            if self.store.conn.execute("SELECT 1 FROM autopilot_log WHERE action='review' AND detail LIKE ?",
                                       (f"post {r['pid']} %",)).fetchone():
                continue
            score = post_score(r["impressions"], r["reactions"], r["comments"], r["reposts"], baseline)
            avg = sum(scores) / len(scores) if scores else score
            verdict = "above" if score >= avg else "below"
            rank = StyleBandit.from_history(scored).ranking()
            top = ", ".join(f"{k} ({m:.2f})" for k, m, _ in [x for x in rank if x[0] in IPO_ARMS][:3])
            text = (f"post {r['pid']} [{r['style']}/{r['hook']}]: {r['impressions']} impressions, {r['reactions']} reactions, "
                    f"{r['comments']} comments, {r['reposts']} reposts -> score {score:.2f}, {verdict} the running average "
                    f"{avg:.2f}. Next posts favour: {top}")
            self.store.add_log("review", text)
            decisions.append(text)
        return decisions

    def weekly_digest_if_due(self) -> str | None:
        """Write the weekly digest on the first cycle of each ISO week."""
        week = date.today().strftime("%G-W%V")
        if self.store.get_kv("last_digest", "") == week:
            return None
        path = self.svc.weekly_digest()["file"]
        self.store.set_kv("last_digest", week)
        return path

    def token_days_left(self) -> float | None:
        """Days until the LinkedIn access token expires, or None if there is no usable token file."""
        import json

        path = self.cfg.token_path
        if not path.exists():
            return None
        tok = json.loads(path.read_text())
        if not tok.get("obtained_at") or not tok.get("expires_in"):
            return None
        return (tok["obtained_at"] + tok["expires_in"] - time.time()) / 86400

    def backup_db(self) -> str | None:
        """One consistent copy of the database per day in data/backups (never deleted: analytics are kept for the ML work)."""
        import sqlite3

        day = date.today().isoformat()
        if self.store.get_kv("last_backup", "") == day:
            return None
        dest_dir = self.cfg.data_dir / "backups"
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / f"growth-{day}.db"
        out = sqlite3.connect(str(dest))
        try:
            self.store.conn.backup(out)
        finally:
            out.close()
        self.store.set_kv("last_backup", day)
        return str(dest)

    # --- owner-scheduled publish (runs inside the server, so Claude Code does not need to be open) ---
    def schedule_publish(self, draft_id: int, at_ts: float) -> dict[str, Any]:
        import hashlib

        d = self.svc._pending(draft_id)
        sha = hashlib.sha1(d["text"].encode("utf-8")).hexdigest()
        items = [i for i in (self.store.get_kv("scheduled_publishes", []) or []) if i["draft_id"] != draft_id]
        items.append({"draft_id": draft_id, "at": at_ts, "sha": sha})
        self.store.set_kv("scheduled_publishes", items)
        self.store.add_log("scheduled_publish_set", f"draft {draft_id} at {datetime.fromtimestamp(at_ts):%Y-%m-%d %H:%M}", draft_id=draft_id)
        return {"draft_id": draft_id, "publish_at": datetime.fromtimestamp(at_ts).isoformat(timespec="minutes")}

    def publish_scheduled(self) -> list[dict[str, Any]]:
        """Publish owner-scheduled drafts that are due. The text must be unchanged since it was scheduled; retries for
        3 hours after the due time (token or network trouble), then drops it and alerts."""
        import hashlib

        items = self.store.get_kv("scheduled_publishes", []) or []
        if not items or self.mode() == "off":
            return []
        now, keep, out = time.time(), [], []
        for it in items:
            if now < it["at"]:
                keep.append(it)
                continue
            did = it["draft_id"]
            try:
                d = self.svc._pending(did)
                if hashlib.sha1(d["text"].encode("utf-8")).hexdigest() != it["sha"]:
                    raise RuntimeError("draft text changed after it was scheduled")
                if not self.token_ready():
                    raise RuntimeError("LinkedIn token not ready")
                res = self.svc.approve_and_publish(did, True)
                self.store.add_log("scheduled_publish", f"draft {did} published: {res['url']}", draft_id=did)
                self._alert("scheduled_publish", "Scheduled LinkedIn post published", res["url"])
                out.append({"draft_id": did, **res})
            except Exception as e:
                msg = str(e)[:200]
                unrecoverable = "changed" in msg or "not pending" in msg
                if unrecoverable or now > it["at"] + 3 * 3600:
                    self.store.add_log("scheduled_publish_dropped", f"draft {did}: {msg}", draft_id=did)
                    self._alert("scheduled_publish", "Scheduled LinkedIn post NOT published", f"draft {did}: {msg}")
                else:
                    self.store.add_log("scheduled_publish_retry", f"draft {did}: {msg}", draft_id=did)
                    keep.append(it)
        self.store.set_kv("scheduled_publishes", keep)
        return out

    def tick(self) -> dict[str, Any]:
        """One autopilot cycle. A lock file stops overlapping cycles (scheduler + scheduled task) from double-posting."""
        import os

        lock = self.cfg.data_dir / "tick.lock"
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                owner = int(lock.read_text().strip() or 0)
            except (OSError, ValueError):
                owner = 0
            if owner and owner != os.getpid() and _pid_alive(owner) and time.time() - lock.stat().st_mtime < 1800:
                return {"mode": self.mode(), "busy": "another cycle is running"}
            lock.unlink(missing_ok=True)  # owner is gone (crash or restart) or the lock is over 30 minutes old
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return self._tick()
        finally:
            lock.unlink(missing_ok=True)

    def _tick(self) -> dict[str, Any]:
        mode = self.mode()
        if mode == "off":
            return {"mode": "off"}
        out: dict[str, Any] = {"mode": mode}
        try:
            left = self.token_days_left()
            if left is not None and left <= 7:
                self._alert("token_expiry", "LinkedIn posting authorization expires soon",
                            f"{max(left, 0):.0f} days left. Re-run the one-time sign-in: uv run -m linkedin_growth_mcp --authorize-linkedin")
        except Exception as e:
            self.store.add_log("token_check_error", str(e)[:200])
        try:
            self.weekly_digest_if_due()
        except Exception as e:
            self.store.add_log("digest_error", str(e)[:200])
        try:
            self.track_followers_if_due()
        except Exception as e:
            self.store.add_log("followers_error", str(e)[:200])
        try:
            self.watch_comments()
        except Exception as e:
            self.store.add_log("replies_error", str(e)[:200])
        try:
            self.auto_experiments_if_due()
        except Exception as e:
            self.store.add_log("experiment_error", str(e)[:200])
        try:
            self.backup_db()
        except Exception as e:
            self.store.add_log("backup_error", str(e)[:200])
        try:
            self.expire_stale_drafts()
        except Exception as e:
            self.store.add_log("expire_error", str(e)[:200])
        last = self.store.get_kv("last_sync", 0)
        if time.time() - last >= SYNC_EVERY_HOURS * 3600:
            try:
                out["sync"] = self.sync_filings()
            except Exception as e:
                self.store.add_log("sync_error", str(e))
                out["sync_error"] = str(e)
        try:  # hourly: append every young post's live analytics + audience, and note whether the saved login still works
            if time.time() - self.store.get_kv("last_analytics_snapshot", 0) >= 3600:
                self.store.set_kv("last_analytics_snapshot", time.time())
                snap = asyncio.run(self.svc.snapshot_analytics())
                out["analytics_snapshot"] = {"saved": len(snap["saved"]), "failed": len(snap["failed"])}
                self.store.add_log("analytics_snapshot", f"{len(snap['saved'])} posts saved, {len(snap['failed'])} failed"
                                   + (" (LinkedIn login expired)" if snap["session_expired"] else "")
                                   + (f": {snap['failed'][0]['error']}" if snap["failed"] else ""))
                if snap["session_expired"]:
                    self._alert("session_expired", "LinkedIn browser login expired",
                                "Hourly analytics cannot be read. Run: uv run -m linkedin_growth_mcp --login")
        except Exception as e:
            self.store.add_log("analytics_snapshot_error", str(e)[:200])
        try:  # market-wide IPO data (chittorgarh): plain HTTP, no Gemini; hourly at most
            if self.cfg.market_sync and time.time() - self.store.get_kv("last_market_sync", 0) >= 3600:
                self.store.set_kv("last_market_sync", time.time())
                out["market"] = self.svc.sync_market()
        except Exception as e:
            self.store.add_log("market_sync_error", str(e)[:200])
        try:
            reviews = self.review_results()  # yesterday's post is scored before today's is drafted
            if reviews:
                out["reviews"] = reviews
        except Exception as e:
            self.store.add_log("review_error", str(e)[:300])
        cand = self.next_candidate()
        if not cand:
            out["step"] = None
            try:  # no IPO post due: on weekends the weekly listing scorecard may go out
                out["weekly"] = self.run_weekly()
                if isinstance(out["weekly"], dict):
                    self._maybe_alert(out["weekly"], mode)
            except Exception as e:
                self.store.add_log("weekly_error", str(e)[:300])
            try:  # nothing else is due: prepare a work-story draft for the owner's approval so the day is not empty
                out["pillar"] = self.pillar_draft_if_due()
            except Exception as e:
                self.store.add_log("pillar_error", str(e)[:300])
            return out
        row, data, step = cand
        if mode == "live":
            now = datetime.now()
            slot = self.post_hour()
            if now.hour < slot:
                if now.hour * 60 + now.minute >= slot * 60 - DRAFT_LEAD_MINUTES:
                    try:  # inside the lead window: draft and check now, publish exactly this text at the posting hour
                        out["lead_draft"] = self.preview_next()
                    except Exception as e:
                        self.store.add_log("lead_draft_error", str(e)[:200])
                out["step"] = {"action": "waiting", "why": f"posting window opens at {slot}:00"}
                return out
            if not self.token_ready():  # do not spend Gemini requests drafting a post that cannot be published
                day_start = time.mktime(date.today().timetuple())
                if not self.store.step_logged(row["id"], step, ("needs_reauth",), since_ts=day_start):
                    self.store.add_log("needs_reauth", "LinkedIn is not authorized; complete the one-time sign-in",
                                       ipo_id=row["id"], step=step)
                out["step"] = {"action": "needs_reauth"}
                return out
        out["step"] = {"company": row["company"], "step": step, **self.run_step(row, data, step)}
        self._maybe_alert(out["step"], mode)
        return out

    def _key_ready(self, purpose: str) -> bool:
        """True when this job has its own key. Otherwise the job is paused (logged once a day), never rerouted to another key."""
        from .gemini.keys import PURPOSES

        if self.cfg.key_for(purpose):
            return True
        day = date.today().isoformat()
        if self.store.get_kv(f"key_pending_{purpose}", "") != day:
            self.store.set_kv(f"key_pending_{purpose}", day)
            self.store.add_log("key_pending", f"{PURPOSES[purpose]['label']} is paused: add {PURPOSES[purpose]['env']} to .env "
                               f"(account: {self.cfg.account_for(purpose) or 'not recorded'})")
        return False

    def is_post_day(self, day: date | None = None) -> bool:
        """Weekdays only, minus listed low-activity holidays (strategy.experiments.HOLIDAYS plus kv "skip_dates")."""
        from .strategy import experiments as ex

        return ex.is_post_day(day or date.today(), self.store.get_kv("skip_dates", []))

    def trial_cell(self, day: date | None = None) -> dict[str, Any] | None:
        """Today's (format, slot) in the running trial, assigned once and remembered. None outside the trial or on a skip day."""
        from .strategy import experiments as ex

        day = day or date.today()
        state = self.store.get_kv("experiments", None)
        trial = state.get("trial") if state else None
        if not trial or day.isoformat() < trial["started"] or not self.is_post_day(day):
            return None
        if day.isoformat() not in trial["days"]:
            ex.cell_for_day(trial, day.isoformat())
            self.store.set_kv("experiments", state)
        return trial["days"][day.isoformat()]

    def format_today(self) -> str:
        """'short' or 'long': today's cell in the trial, else the decided format."""
        cell = self.trial_cell()
        return cell["format"] if cell else self.store.get_kv("format_mode", "long")

    def slot_hours(self) -> tuple[int, ...]:
        """Candidate posting hours: the experiment's override if one is set, otherwise the configured ones."""
        override = self.store.get_kv("post_hours", None)
        if override:
            return tuple(int(h) for h in override)
        return self.cfg.post_hours or (self.cfg.post_hour,)

    def post_hour(self) -> int:
        """Today's posting hour. With one configured slot this is that hour; with several the bandit picks one a day."""
        cell = self.trial_cell()
        if cell:  # the trial assigns today's slot
            return int(cell["slot"])
        hours = self.slot_hours()
        if len(hours) == 1:
            return hours[0]
        key = "post_hour_" + date.today().isoformat()
        chosen = self.store.get_kv(key)
        if chosen in hours:
            return int(chosen)
        rows = [(datetime.fromtimestamp(r["published_at"]).hour, r) for r in self.store.hour_scores()]
        imps = [r["impressions"] for _, r in rows]
        baseline = sum(imps) / len(imps) if imps else 0.0
        history = [(h, post_score(r["impressions"], r["reactions"], r["comments"], r["reposts"], baseline))
                   for h, r in rows]
        chosen = SlotBandit.from_history(tuple(hours), history).choose()
        self.store.set_kv(key, chosen)
        self.store.add_log("slot_chosen", f"posting hour {chosen}:00 chosen from {list(hours)}")
        return int(chosen)

    def posting_time_report(self) -> dict[str, Any]:
        hours = self.slot_hours()
        rows = [(datetime.fromtimestamp(r["published_at"]).hour, r) for r in self.store.hour_scores()]
        imps = [r["impressions"] for _, r in rows]
        baseline = sum(imps) / len(imps) if imps else 0.0
        history = [(h, post_score(r["impressions"], r["reactions"], r["comments"], r["reposts"], baseline))
                   for h, r in rows]
        rank = SlotBandit.from_history(tuple(hours), history).ranking()
        return {
            "candidate_hours": list(hours),
            "today": self.post_hour(),
            "learning": len(hours) > 1,
            "by_hour": [{"hour": h, "mean_score": round(m, 3), "measured_posts": n} for h, m, n in rank],
            "note": ("Only one hour is configured, so nothing is being learned. Set POST_HOURS (e.g. 8,9,13,18) to "
                     "let the bandit test slots." if len(hours) == 1 else
                     "Each day one hour is sampled; hours with better scores are chosen more often."),
        }

    def auto_experiments_if_due(self, today: date | None = None) -> dict[str, Any] | None:
        """Runs the concurrent format + posting-time trial by itself (RULEBOOK E): assigns each post day a cell, judges both
        factors as posts mature, freezes each as soon as its verdict is final, and archives the trial when both are.

        Locked until strategy.experiments.START_AFTER (the owner's decision). Images stay off by the owner's choice."""
        from .strategy import experiments as ex

        if not self.cfg.auto_experiments:
            return None
        today = today or date.today()
        if not ex.can_start(today):
            return None
        day = today.isoformat()
        if self.store.get_kv("last_hook_analysis", "") != day:  # once a day, no Gemini: the opening-pattern record keeps growing
            self.store.set_kv("last_hook_analysis", day)
            rep = self.svc.hook_report()
            self.store.add_log("hook_analysis", f"{rep['posts']} posts; " + ", ".join(
                f"{g['pattern']} x{g['posts']} avg {g['avg_impressions']}" for g in rep["by_pattern"]) + f". {rep['verdict']}")
        state = self.store.get_kv("experiments", None) or ex.new_state()
        if not state["trial"]:
            if state["history"]:
                return None  # one trial only; its outcome is now the setting
            newest = self.store.conn.execute("SELECT COALESCE(MAX(id), 0) m FROM posts").fetchone()["m"]
            ex.start_trial(state, today, newest + 1)
            self.store.set_kv("experiments", state)
            self.store.add_log("experiment_started", "format (long vs short) and slot (9, 13, 18) run side by side on weekdays; "
                               f"format first look at {ex.FORMAT_MIN} posts per arm, slot at {ex.SLOT_MIN} per slot")
        self.trial_cell(today)  # today's cell is fixed before the day's draft is made
        state = self.store.get_kv("experiments")
        ev = self.svc.experiment_status(today)["evaluation"]
        decided = ex.decide(state["trial"], ev, today)
        for f in decided:
            d = state["trial"]["decided"][f]
            if f == "format":
                self.store.set_kv("format_mode", d["value"])
            else:
                self.store.set_kv("post_hours", [int(d["value"])])
            r = d["result"]
            self.store.add_log("experiment_result", f"{f} {d['verdict']}: {d['value']} - lift {r.get('lift_pct')}%, p={r.get('p')}, "
                               f"posts {r.get('posts')}. {r['note']}")
        finished = ex.finish(state, today)
        if decided or finished:
            self.store.set_kv("experiments", state)
        if finished:
            self.store.add_log("experiment_finished", "both the format and the posting slot are decided; the trial is archived")
        return {"decided": decided, "finished": finished} if decided or finished else None

    def track_followers_if_due(self) -> int | None:
        """One follower reading a day: the account-level number that post impressions cannot show."""
        import asyncio

        day = date.today().isoformat()
        if self.store.get_kv("last_followers", "") == day:
            return None
        self.store.set_kv("last_followers", day)  # one attempt a day, success or not, so a failure cannot loop
        res = asyncio.run(self.svc.track_followers())
        self.store.add_log("followers", f"{res['followers']} followers"
                           + (f" ({res['change']:+d} since the last reading)" if res["change"] is not None else ""))
        return int(res["followers"])

    def watch_comments(self) -> dict[str, Any] | None:
        """Score the conversation under the newest young post: comment quality, and how well the owner replied.

        No reply is drafted or sent: the owner writes those. This only keeps track of how good the exchanges are.
        """
        import asyncio

        if not self._key_ready("insights"):
            return None
        posts = [p for p in self.store.list_posts()
                 if p["url"] and time.time() - p["published_at"] <= self.cfg.comment_watch_hours * 3600]
        if not posts:
            return None
        p = max(posts, key=lambda r: r["published_at"])
        key = f"comments_checked_{p['id']}"
        if time.time() - float(self.store.get_kv(key, 0)) < self.cfg.comment_check_minutes * 60:
            return None
        self.store.set_kv(key, time.time())
        before = self.store.latest_conversation(p["id"])
        res = asyncio.run(self.svc.check_comments(p["id"]))
        if not res.get("analysed"):
            return res
        unanswered = max(res["comments"] - res["author_replies"], 0)
        self.store.add_log("conversation", f"post {p['id']}: {res['comments']} comments (quality "
                           f"{res['comment_quality']}), {res['author_replies']} replies by you (quality "
                           f"{res['reply_quality']}), {unanswered} unanswered")
        if unanswered and (before is None or res["comments"] > before["comments"]):
            self._alert("comments", "New comment on your LinkedIn post",
                        f"{unanswered} comment(s) with no reply yet. Replying within the hour is the biggest reach lever.")
        return res

    def _editor_score(self, draft_id: int) -> tuple[float, list[str]]:
        """Gemini editor score out of 10 and its weaknesses. A failure scores the draft as acceptable."""
        try:
            rev = self.svc.review_draft(draft_id)
            score = float(rev.get("score") or 0)
            weak = [str(w)[:160] for w in (rev.get("weaknesses") or [])][:4]
            first = rev.get("suggested_rewrite_of_first_line")
            if first:
                weak.append(f"use a stronger first line, for example: {str(first)[:160]}")
            return score, weak
        except Exception as e:
            self.store.add_log("editor_error", str(e)[:200], draft_id=draft_id)
            return float(self.cfg.editor_min_score), []

    def expire_stale_drafts(self) -> int:
        """Delete waiting drafts once the day has its post or the draft is from an earlier day.

        Published posts and their metrics are never touched. A backlog idea tied to the draft goes back to 'backlog'.
        """
        now = time.time()
        today_start = time.mktime(date.today().timetuple())
        posted = self.posted_on(date.today())
        rows = self.store.conn.execute(
            "SELECT id, created_at FROM drafts WHERE status='pending_approval' AND created_at<?", (now - DRAFT_EXPIRY_HOURS * 3600,)
        ).fetchall()
        n = 0
        for r in rows:
            if not (posted or r["created_at"] < today_start):
                continue
            self.store.conn.execute("UPDATE ideas SET status='backlog', draft_id=NULL WHERE draft_id=? AND status='drafted' AND series!='ipo'", (r["id"],))
            self.store.conn.execute(  # the NOT IN guard keeps a published post's draft safe whatever its status says
                "DELETE FROM drafts WHERE id=? AND status='pending_approval' AND id NOT IN (SELECT draft_id FROM posts)",
                (r["id"],))
            self.store.conn.commit()
            self.store.add_log("draft_deleted", f"draft {r['id']} deleted: the day's post is out and it was not approved")
            n += 1
        return n

    def allowed_pillars(self) -> tuple[str, ...]:
        return (*self.cfg.pillar_series, *self.store.get_kv("unlocked_series", []))

    def maybe_unlock_topic(self) -> str | None:
        """Add the next rollout topic when enough posts of the current non-IPO set have been measured and held up."""
        allowed = self.allowed_pillars()
        nxt = next((t for t in TOPIC_ROLLOUT if t not in allowed), None)
        if nxt is None:
            return None
        since = self.store.get_kv("unlock_since", 0)
        if not since:
            self.store.set_kv("unlock_since", time.time())  # only posts from now on count toward the first unlock
            return None
        rows = self.store.latest_metrics()
        if not rows:
            return None
        baseline = sum(r["impressions"] for r in rows) / len(rows)
        mine = [r for r in self.store.series_metrics() if r["series"] in allowed and r["series"] != "ipo" and r["published_at"] >= since]
        if len(mine) < UNLOCK_MIN_POSTS:
            return None

        def sc(r: Any) -> float:
            return post_score(r["impressions"], r["reactions"], r["comments"], r["reposts"], baseline)

        before = [sc(r) for r in rows if r["published_at"] < since]  # every post measured before this test sets the bar to beat
        bar = min(max(sum(before) / len(before), UNLOCK_BAR_RANGE[0]), UNLOCK_BAR_RANGE[1]) if before else UNLOCK_FALLBACK_BAR
        score = sum(sc(r) for r in mine) / len(mine)
        if score < bar:
            self.store.add_log("topic_hold", f"{len(mine)} posts of {','.join(allowed)} scored {score:.2f} < bar {bar:.2f} (your earlier posts); '{nxt}' stays locked")
            return None
        self.store.set_kv("unlocked_series", [*self.store.get_kv("unlocked_series", []), nxt])
        self.store.set_kv("unlock_since", time.time())
        self.store.add_log("topic_unlocked", f"'{nxt}' added after {len(mine)} measured posts scored {score:.2f} vs bar {bar:.2f}")
        self._alert("topic", "LinkedIn autopilot added a new topic", f"'{nxt}' unlocked: the current topics scored {score:.2f}, above the bar of {bar:.2f}.")
        return nxt

    def topic_order(self) -> list[str]:
        """Topic groups for the next non-IPO post, most-deserving first (weekly mix deficit, past results, 30-day floor)."""
        now = time.time()
        rows = self.store.conn.execute(
            "SELECT i.series, p.published_at FROM posts p JOIN ideas i ON i.draft_id=p.draft_id").fetchall()
        counts: dict[str, int] = {}
        last: dict[str, float] = {}
        for r in rows:
            g = topics.GROUP_OF.get(r["series"])
            if not g:
                continue
            last[g] = max(last.get(g, 0.0), r["published_at"])
            if r["published_at"] >= now - 7 * 86400:
                counts[g] = counts.get(g, 0) + 1
        metrics = self.store.series_metrics()
        base = (sum(r["impressions"] for r in metrics) / len(metrics)) if metrics else 500.0
        by_group: dict[str, list[float]] = {}
        for r in metrics:
            g = topics.GROUP_OF.get(r["series"])
            if g:
                by_group.setdefault(g, []).append(post_score(r["impressions"], r["reactions"], r["comments"], r["reposts"], base))
        perf = {g: sum(v) / len(v) for g, v in by_group.items() if v}
        days_since = {g: (now - t) / 86400 for g, t in last.items()}
        return topics.rank_groups(counts, perf, days_since, set(self.allowed_pillars()))

    def _recent_post_lines(self, n: int = 6) -> list[str]:
        """The last n published posts: opening line plus latest impressions / reactions / comments / reposts."""
        rows = self.store.post_texts()[-n:]
        out = []
        for r in rows:
            first = next((ln.strip() for ln in r["text"].splitlines() if ln.strip()), "")[:110]
            out.append(f"- \"{first}\" -> {r['impressions']} impressions, {r['reactions']} reactions, "
                       f"{r['comments']} comments, {r['reposts']} reposts")
        return out or ["- (none yet)"]

    def autofill_ideas_if_due(self) -> int:
        """Once a week (or when fewer than 3 backlog ideas remain) ask the model for 6-8 new idea titles built from the
        owner's story bank, audience and results. One call on the insights key; every idea is validated and deduplicated."""
        week = date.today().strftime("%G-W%V")
        allowed = set(self.allowed_pillars())
        backlog = [i for i in self.store.list_ideas(status="backlog") if i["series"] in allowed]
        if self.store.get_kv("autofill_week", "") == week and len(backlog) >= 3:
            return 0
        if not self._key_ready("insights"):
            return 0
        self.store.set_kv("autofill_week", week)
        bank = (self.store.get_kv("story_bank", "") or "").strip()
        ints = self.store.get_kv("interests") or {}
        titles = {i["title"] for i in self.store.list_ideas()}
        recent = [i["title"] for i in self.store.list_ideas(status="published")][-8:]
        prompt = (
            "Propose 6 to 8 LinkedIn post ideas for this analyst. Audience priority: (1) recruiters, hiring managers and analyst "
            "peers; (2) college students and freshers wanting analyst jobs. Series allowed: "
            f"{sorted(allowed - {'ipo'})} ('career' = advice for freshers from the owner's own experience; 'story' = a real "
            "work story; 'bi_features' = Power BI / Looker Studio; 'ai' = AI tools for analysts). Spread across series; at least "
            "2 for 'career'. Each idea must be buildable from the STORY BANK or from widely documented facts: never assume a "
            "number, employer detail or result that is not in the bank. No dashes, no links, no names of people. "
            "Return JSON: {\"ideas\": [{\"series\": str, \"title\": str (max 100 chars), \"brief\": str (1-2 sentences: the angle "
            "and which bank facts to use)}]}."
        ) + "\n\n" + "\n".join([
            f"AUDIENCE/GOAL: {ints.get('audience', '')} | {ints.get('goal', '')}",
            f"TOPICS: {ints.get('topics', '')}",
            f"ALREADY POSTED OR QUEUED (do not repeat): {sorted(titles)[:40]}",
            f"RECENT: {recent}",
            "",
            "YOUR LAST POSTS WITH RESULTS (learn what landed; avoid repeating their angle; do not copy numbers from here):",
            *self._recent_post_lines(6),
            "",
            "STORY BANK:",
            bank[:9000],
        ])
        raw = self.svc.insights_text._json(prompt)  # counted against the insights key's own daily cap inside _generate
        banned = self.store.get_kv("banned_terms", []) or []
        fresh = topics.clean_ideas(raw, allowed, titles, lambda t: bool(gates.banned_term_hits(t, banned)))
        for it in fresh:
            self.store.add_idea(it["series"], it["title"], it["brief"], 2, "")
        self.store.add_log("ideas_autofill", f"{len(fresh)} new ideas: " + "; ".join(f"{i['series']}: {i['title']}" for i in fresh))
        return len(fresh)

    def pillar_draft_if_due(self) -> dict[str, Any] | None:
        """On a day with no IPO or scorecard post, draft ONE non-IPO backlog post, gate it, and publish it.

        The gates check form and safety only (placeholders, links, advice wording, length, dashes): there is no
        verified-facts pack for work stories, so the owner has chosen to accept that risk.
        """
        today = date.today()
        if not self.is_post_day(today) or self.mode() != "live" or datetime.now().hour < self.post_hour() or self.posted_on(today):
            return None
        if self.store.get_kv("pillar_drafted", "") == today.isoformat():
            return None
        day_start = time.mktime(today.timetuple())
        if self.store.posts_published_since(day_start) > 0:  # anything published today (e.g. an owner-scheduled post) uses up the day
            return None
        if any(day_start <= i["at"] < day_start + 86400 for i in (self.store.get_kv("scheduled_publishes", []) or [])):
            return None  # the owner scheduled a post for today: do not add a second one
        if (WEEKLY_SCORECARD_ENABLED and today.weekday() in (5, 6) and self.store.get_kv("last_weekly", "") != today.strftime("%G-W%V")
                and self.store.get_kv("weekly_tried", "") != today.isoformat()):
            return None  # the weekend scorecard has not had its turn yet; a work story must not use up the day first
        self.maybe_unlock_topic()
        try:
            self.autofill_ideas_if_due()
        except Exception as e:
            self.store.add_log("autofill_error", str(e)[:200])
        order = self.topic_order()
        ideas = sorted((i for i in self.store.list_ideas(status="backlog") if i["series"] in self.allowed_pillars()),
                       key=lambda i: (order.index(topics.GROUP_OF.get(i["series"], "")) if topics.GROUP_OF.get(i["series"]) in order else 99,
                                      i["priority"], i["id"]))
        if not ideas:
            return {"action": "skipped", "why": "no ideas left in the allowed topics"}
        if not self.token_ready():
            return {"action": "needs_reauth", "company": "Pillar post"}
        self.store.set_kv("pillar_drafted", today.isoformat())  # one attempt per day, success or not, so a failure cannot burn the quota hourly
        idea = ideas[0]
        reasons: list[str] = []
        draft = None
        best: tuple[float, dict[str, Any], int] | None = None
        for _ in range(MAX_BLOCKED_ATTEMPTS):
            cand = self.svc.propose_post(idea["id"], hints=reasons or None)
            tidy = polish(cand["text"])
            if tidy != cand["text"]:
                self.store.update_draft(cand["draft_id"], text=tidy)
                cand["text"] = tidy
            reasons = gates.check_general(cand["text"], self.store.get_kv("banned_terms", []) or [], self.svc.idea_facts(idea))
            if not reasons:
                self._log_ai_tells(cand)
                keep, score, reasons = self._editor_round(cand, best)
                if keep is not None:
                    draft = keep
                    break
                best = (score, cand, cand["draft_id"]) if best is None or score > best[0] else best
                continue
            self.store.update_draft(cand["draft_id"], status="blocked")
        if draft is None and best is not None:
            self.store.add_log("editor_best_effort", f"draft {best[2]} published at editor score {best[0]:.1f}", draft_id=best[2])
            self.store.update_draft(best[2], status="pending_approval")
            draft = best[1]
        if draft is None:
            self.store.add_log("blocked", f"{idea['title']}: " + "; ".join(reasons), step="pillar")
            return {"action": "blocked", "company": "Pillar post", "reasons": reasons}
        try:
            res = self.svc.approve_and_publish(draft["draft_id"], True)
        except Exception as e:
            self.store.add_log("error", str(e), step="pillar", draft_id=draft["draft_id"])
            return {"action": "error", "company": "Pillar post", "error": str(e)}
        self.store.add_log("published", res.get("url") or "", step="pillar", draft_id=draft["draft_id"])
        return {"action": "published", "company": "Pillar post", **res}

    def run_weekly(self, today: date | None = None) -> dict[str, Any] | None:
        """Saturday (Sunday as fallback): post the scorecard of IPOs that listed in the past 7 days, once per ISO week."""
        today = today or date.today()
        mode = self.mode()
        if not WEEKLY_SCORECARD_ENABLED or mode == "off" or today.weekday() not in (5, 6):
            return None
        week = today.strftime("%G-W%V")
        if self.store.get_kv("last_weekly", "") == week:
            return None
        if datetime.now().hour < self.post_hour() + 1:  # an hour after the usual slot
            return {"action": "waiting", "why": "weekly scorecard goes out an hour after the posting hour"}
        if self.posted_on(today):
            return {"action": "deferred", "why": "already posted today"}
        if self.store.get_kv("weekly_tried", "") == today.isoformat():
            return {"action": "waiting", "why": "weekly scorecard was already attempted today"}
        if mode == "live" and not self.token_ready():
            return {"action": "needs_reauth", "company": "Weekly scorecard"}
        pack = self.svc.weekly_listing_facts(today)
        if pack["n"] < WEEKLY_MIN_IPOS:
            self.store.set_kv("weekly_tried", today.isoformat())
            self.store.add_log("weekly_skip", f"only {pack['n']} listings in the last 7 days (need {WEEKLY_MIN_IPOS})", step="weekly")
            return {"action": "skipped", "why": f"only {pack['n']} listings this week"}
        self.store.set_kv("weekly_tried", today.isoformat())  # one attempt per day, success or not, so a failure cannot burn the quota hourly
        title = f"Weekly IPO scorecard {week}"
        existing = next((i for i in self.store.list_ideas("ipo") if i["title"] == title), None)
        if existing:
            self.store.update_idea(existing["id"], facts=pack["facts"])
            idea_id = existing["id"]
        else:
            idea_id = self.store.add_idea("ipo", title, BRIEFS["weekly"], priority=1, facts=pack["facts"])
        draft, reasons, last_id = self._draft_passing(idea_id, pack["facts"])
        if draft is None:
            self.store.add_log("blocked", "; ".join(reasons), step="weekly", draft_id=last_id)
            return {"action": "blocked", "company": "Weekly scorecard", "reasons": reasons}
        did = draft["draft_id"]
        if mode != "live":
            self.store.update_draft(did, status="shadow_pass")
            self.store.add_log("shadow_pass", f"{title}: all gates passed (not published: mode={mode})", step="weekly", draft_id=did)
            return {"action": "shadow_pass", "draft_id": did}
        try:
            res = self.svc.approve_and_publish(did, True)
        except Exception as e:
            msg = str(e)
            action = "needs_reauth" if ("token" in msg.lower() or "authorized" in msg.lower()) else "error"
            self.store.add_log(action, msg, step="weekly", draft_id=did)
            return {"action": action, "company": "Weekly scorecard", "error": msg}
        self.store.set_kv("last_weekly", week)
        self.store.add_log("published", res.get("url") or "", step="weekly", draft_id=did)
        return {"action": "published", "company": "Weekly scorecard", **res}

    def _alert(self, key: str, title: str, text: str) -> None:
        """Log and show at most one desktop alert per kind per day."""
        from .notify import notify

        day = date.today().isoformat()
        if self.store.get_kv(f"alert_{key}", "") == day:
            return
        self.store.set_kv(f"alert_{key}", day)
        self.store.add_log("alert", f"{title}: {text}")
        if self.cfg.alerts:
            notify(title, text)

    def _maybe_alert(self, step: dict[str, Any], mode: str) -> None:
        action = step.get("action")
        company = step.get("company", "the next IPO")
        if action in ("error", "needs_reauth", "blocked"):
            self._alert(action, "LinkedIn autopilot needs attention", f"{company}: {action}. {str(step.get('error') or step.get('why') or '')[:120]}")
        elif mode == "live" and action != "published" and datetime.now().hour >= 11 and not self.posted_on(date.today()):
            self._alert("missed", "No LinkedIn post yet today", f"{company} is still waiting to be posted (status: {action}).")

    # ---- reporting
    def status(self) -> dict[str, Any]:
        from .linkedin import oauth

        try:
            tok = oauth.load_token(self.cfg)
            token = f"ok, expires in ~{int((tok['obtained_at'] + int(tok.get('expires_in', 0)) - time.time()) / 86400)} days"
        except Exception as e:
            token = f"NOT READY: {e}"
        logs = [{"time": datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"), "action": r["action"],
                 "step": r["step"], "detail": r["detail"][:160], "draft_id": r["draft_id"]} for r in self.store.list_logs(25)]
        ipos = []
        for r in self.store.list_ipos():
            d = json.loads(r["data"])
            ipos.append({"ipo_id": r["id"], "company": r["company"], "verified": bool(r["verified"]),
                         "opens": (d.get("issue") or {}).get("open_date"), "closes": (d.get("issue") or {}).get("close_date")})
        shadow = self.store.conn.execute("SELECT COUNT(*) c FROM drafts WHERE status='shadow_pass'").fetchone()["c"]
        blocked = self.store.conn.execute("SELECT COUNT(*) c FROM autopilot_log WHERE action='blocked'").fetchone()["c"]
        return {"mode": self.mode(), "linkedin_token": token, "linkedin_browser_session": self.svc.session_state(),
                "shadow_passed_drafts": shadow, "blocked_events": blocked,
                "ipos": ipos, "image_requests": self.image_requests(), "recent": logs}
