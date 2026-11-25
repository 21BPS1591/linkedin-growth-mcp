"""Find well-performing posts in the owner's niche and comment on them (fully automatic, capped per day).

All LinkedIn page knowledge lives here, so breakage is easy to fix. Selection and the comment checks are plain functions
with no browser, so they can be tested. The comment text itself comes from the caller (Gemini on the insights key).
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
from typing import Any, Callable
from urllib.parse import quote

from patchright.async_api import async_playwright

from .browser import LinkedInBrowser, LoginExpired
from .publish import strip_dashes

# Searches rotate by day so the comments are spread over the owner's whole domain.
KEYWORDS = (
    "power bi dashboard", "looker studio", "financial modelling excel", "data analyst career",
    "business analyst", "excel dashboard", "dax power bi", "sql for analysts", "ai for data analysts",
    "fresher data analyst", "kpi dashboard", "financial analysis",
)
from pathlib import Path

DEBUG_SHOT = Path(__file__).resolve().parents[3] / "data" / "engage_fail.png"
SEARCH = "https://www.linkedin.com/search/results/content/?keywords={q}&datePosted=%22past-week%22&sortBy=%22relevance%22"

MIN_REACTIONS = 25      # "getting great impressions": reactions are the visible proxy for reach
MAX_COMMENTS = 60       # a post with a big thread buries a new comment
MAX_AGE_HOURS = 96      # a comment on an old post is read by nobody
MIN_BODY_CHARS = 200
MAX_WORDS, MIN_WORDS = 60, 8
MIN_JUDGE_SCORE = 8     # the editor pass must rate a comment at least this (1 to 10) or it is not posted
MAX_QUESTION_STREAK = 1  # after this many question-ending comments in a row, the next must not end with one
AUTHOR_COOLDOWN_DAYS = 7

_AGE = re.compile(r"^(\d+)\s*(m|h|d|w|mo|yr)\b", re.IGNORECASE)
_DEGREE = re.compile(r"•\s*(1st|2nd|3rd\+?)")
_SKIP_POST = re.compile(
    r"\b(hiring|we are looking for|apply (now|here)|job opening|vacancy|giveaway|webinar|register (now|here)|"
    r"comment (below )?to (get|receive)|dm me|link in (the )?comments?|course|bootcamp|rest in peace|RIP|passed away|"
    r"condolence|layoff|laid off|election|politic|religio)\w*", re.IGNORECASE)
_SKIP_AUTHOR = re.compile(r"recruit|talent acquisition|hr|hiring|staffing|headhunt|career coach|placement", re.IGNORECASE)
_COMMON = frozenset("which where would could should about their there these those other using often always never really helps works great while still every because before after between through across within without often usually simple clear better easier faster matter things thing makes check build"              .split())
_GENERIC_OPEN = re.compile(r"^\W*(great|nice|good|awesome|amazing|love this|insightful|thanks for sharing|well said|congrat)", re.IGNORECASE)
_LINKS = re.compile(r"https?://|www\.|@\w|#\w")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def keywords_for(day_ordinal: int, run: int = 0, n: int = 3) -> list[str]:
    start = (day_ordinal * 5 + run * n) % len(KEYWORDS)
    return [KEYWORDS[(start + k) % len(KEYWORDS)] for k in range(n)]


def card_key(author: str, body: str) -> str:
    return hashlib.sha1(f"{author.strip().lower()}|{' '.join(body.split())[:120].lower()}".encode()).hexdigest()[:16]


def _age_hours(token: str) -> float | None:
    m = _AGE.match(token.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"m": 1 / 60, "h": 1, "d": 24, "w": 168, "mo": 720, "yr": 8760}[unit]


def parse_card(text: str, reactions: int, comments: int, reposts: int) -> dict[str, Any] | None:
    """One 'Feed post' block (its visible text plus the three counts read from its buttons) -> a candidate, or None."""
    lines = [ln.strip() for ln in text.replace("​", "").splitlines()]
    lines = [ln for ln in lines if ln]
    if lines and lines[0] == "Feed post":
        lines = lines[1:]
    age_i = next((i for i, ln in enumerate(lines[:10]) if _age_hours(ln) is not None and "•" in ln), None)
    if age_i is None or not lines:
        return None
    author = lines[0]
    head = " ".join(lines[1:age_i])
    body = "\n".join(ln for ln in lines[age_i + 1:] if ln not in ("Follow", "…more", "… more", "Subscribe"))
    body = re.sub(r"(\n\d+)+\s*$", "", body).strip()
    return {
        "key": card_key(author, body), "author": author, "headline": head[:160], "is_person": bool(_DEGREE.search(head)),
        "age_h": _age_hours(lines[age_i]), "text": body[:3000], "reactions": reactions, "comments": comments, "reposts": reposts,
    }


def pick_candidates(cards: list[dict[str, Any]], *, owner: str, done_keys: set[str], recent_authors: set[str],
                    banned: list[str], limit: int = 6) -> list[dict[str, Any]]:
    """Posts worth a comment: a person's recent, original, text-rich post that already has traction. Best first."""
    from ..strategy import gates

    out = []
    seen: set[str] = set()
    for c in cards:
        if not c or c["key"] in done_keys or c["key"] in seen or not c["is_person"]:
            continue
        a = c["author"].strip().lower()
        if a == owner.strip().lower() or a in recent_authors:
            continue
        if _SKIP_AUTHOR.search(c["headline"]):
            continue
        if c["age_h"] is None or c["age_h"] > MAX_AGE_HOURS or c["reactions"] < MIN_REACTIONS or c["comments"] > MAX_COMMENTS:
            continue
        if len(c["text"]) < MIN_BODY_CHARS or _SKIP_POST.search(c["text"]) or gates.banned_term_hits(c["text"], banned):
            continue
        seen.add(c["key"])
        out.append(c)
    # traction first, discounted by age and by how crowded the thread already is
    out.sort(key=lambda c: (c["reactions"] + 2 * c["comments"]) / ((c["age_h"] or 1) + 6) ** 0.5 / (1 + c["comments"] / 80), reverse=True)
    return out[:limit]


def comment_problems(text: str, banned: list[str], recent: list[str], post: str | None = None) -> list[str]:
    """Why a drafted comment must not be posted. Empty list = fine."""
    from ..strategy import aitells, gates

    t = text.strip()
    why: list[str] = []
    n = len(t.split())
    if n < MIN_WORDS or n > MAX_WORDS:
        why.append(f"{n} words (need {MIN_WORDS} to {MAX_WORDS})")
    if _GENERIC_OPEN.search(t):
        why.append("generic opener")
    if _LINKS.search(t) or _EMOJI.search(t):
        why.append("link, mention, hashtag or emoji")
    if gates.banned_term_hits(t, banned):
        why.append("banned term")
    tells = aitells.lint_ai_tells(t)
    if [f for f in tells["flags"] if f != "no_closing_question"]:
        why.append("AI tells: " + ", ".join(f for f in tells["flags"] if f != "no_closing_question")[:80])
    if post is not None:  # grounding: it must visibly come from THIS post, and must not bring in figures the post never gave
        words_c = {w for w in re.findall(r"[a-z]{5,}", t.lower()) if w not in _COMMON}
        words_p = set(re.findall(r"[a-z]{5,}", post.lower()))
        if len(words_c & words_p) < 2:
            why.append("not grounded in the post (shares fewer than 2 distinctive words with it)")
        post_nums = set(re.findall(r"\d+(?:\.\d+)?", post))
        extra = [n for n in re.findall(r"\d+(?:\.\d+)?", t) if n not in post_nums]
        if extra:
            why.append("figure not in the post: " + ", ".join(extra[:3]))
    opener = " ".join(re.findall(r"[a-z']+", t.lower())[:3])
    if opener and any(" ".join(re.findall(r"[a-z']+", r.lower())[:3]) == opener for r in recent[-12:]):
        why.append("same opening words as a recent comment")
    tail = [r.strip().endswith("?") for r in recent[-MAX_QUESTION_STREAK:]]
    if t.endswith("?") and len(tail) == MAX_QUESTION_STREAK and all(tail):
        why.append("too many question endings in a row")
    words = set(re.findall(r"[a-z']+", t.lower()))
    for r in recent:
        rw = set(re.findall(r"[a-z']+", r.lower()))
        if rw and len(words & rw) / len(words | rw) > 0.5:
            why.append("too similar to an earlier comment")
            break
    return why


def tidy_comment(text: str) -> str:
    return " ".join(strip_dashes(text).split())


_CARDS_JS = """()=>[...document.querySelectorAll('div[role=listitem]')].filter(e=>(e.innerText||'').startsWith('Feed post')).map((e,i)=>{e.setAttribute('data-eng',String(i));
  const n=(label)=>{const b=[...e.querySelectorAll('button')].find(b=>(b.getAttribute('aria-label')||'').startsWith(label));
    const t=b?(b.innerText||'').replace(/[^0-9KkMm.,]/g,''):'';return t};
  return {idx:i, text:e.innerText, reactions:n('Reaction button state'), comments:n('Comment'), reposts:n('Repost')}})"""


def _num(s: str) -> int:
    from .analytics import _to_int

    return _to_int(s) if s else 0


async def _cards(page) -> list[dict[str, Any]]:
    raw = await page.evaluate(_CARDS_JS)
    cards = [parse_card(r["text"], _num(r["reactions"]), _num(r["comments"]), _num(r["reposts"])) for r in raw]
    for c, r in zip(cards, raw):
        if c:
            c["idx"] = r["idx"]
    return [c for c in cards if c]


async def _search(page, kw: str) -> None:
    await page.goto(SEARCH.format(q=quote(kw)), wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(4, 6))
    if "/login" in page.url or "/checkpoint" in page.url or "authwall" in page.url:
        raise LoginExpired("LinkedIn session is not logged in (or hit a checkpoint). Run `--login` again.")
    for _ in range(4):
        await page.mouse.wheel(0, random.randint(700, 1000))
        await asyncio.sleep(random.uniform(1.0, 2.0))
    # the feed shows only the start of a long post: expand every "...more" so the whole post is read, not a fragment
    await page.evaluate(r"""()=>{document.querySelectorAll('div[role=listitem] button').forEach(b=>{
      const t=(b.innerText||'').trim(); if(/^(…|\.\.\.)\s*more$/i.test(t)) b.click();});}""")
    await asyncio.sleep(random.uniform(1.5, 2.5))


async def _post_comment(page, c: dict[str, Any], text: str) -> bool:
    """Type the comment into this post's own box and submit. True only when the text is then visible in the post."""
    fresh = next((x for x in await _cards(page) if x["key"] == c["key"]), None)  # re-tag: the page may have reloaded
    if fresh is None:
        return False
    card = page.locator(f'div[role=listitem][data-eng="{fresh["idx"]}"]')
    await card.scroll_into_view_if_needed()
    await asyncio.sleep(random.uniform(1.0, 2.5))
    editor = card.locator(".tiptap.ProseMirror, [aria-label='Text editor for creating comment']").first
    for attempt in range(3):  # the box sometimes needs a second click, or the first click lands before the page is ready
        if await editor.count() == 0:
            await card.get_by_role("button", name="Comment", exact=True).first.click()
        try:
            await editor.wait_for(timeout=6_000)
            break
        except Exception:
            if attempt == 2:
                try:  # leave evidence of what the page looked like
                    await page.screenshot(path=str(DEBUG_SHOT))
                except Exception:
                    pass
                raise
            await asyncio.sleep(random.uniform(1.5, 3.0))
    await editor.click()
    await page.keyboard.type(text, delay=random.randint(25, 70))
    await asyncio.sleep(random.uniform(1.5, 3.0))
    # after typing, the submit button is the one with no aria-label whose text is exactly "Comment"
    submit = card.locator("button:not([aria-label])").filter(has_text=re.compile(r"^\s*Comment\s*$")).last
    await submit.click()
    await asyncio.sleep(random.uniform(3.0, 5.0))
    # posted = the text now shows in the thread and is no longer sitting unsent in the box
    still_typed = [t for t in await card.locator(".tiptap.ProseMirror").all_inner_texts() if text[:40] in t]
    return text[:40] in (await card.inner_text()) and not still_typed


async def run_session(browser: LinkedInBrowser, keywords: list[str], choose: Callable[[list[dict[str, Any]]], list[tuple[dict[str, Any], str]]],
                      owner: str, done_keys: set[str], recent_authors: set[str], banned: list[str], limit: int,
                      on_posted: Callable[[dict[str, Any], str, bool], None], dry_run: bool = False) -> dict[str, Any]:
    """One browser session: search each keyword until enough posts fit, let `choose` draft comments, then post at most
    `limit` of them with human-paced gaps."""
    async with browser._lock:
        async with async_playwright() as pw:
            ctx = await browser._context(pw, headless=True)
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                cards: list[dict[str, Any]] = []
                cands: list[dict[str, Any]] = []
                for kw in keywords:
                    await _search(page, kw)
                    got = await _cards(page)
                    for g in got:
                        g["kw"] = kw
                    cards += got
                    cands = pick_candidates(cards, owner=owner, done_keys=done_keys, recent_authors=recent_authors, banned=banned)
                    if len(cands) >= 5:
                        break
                    await asyncio.sleep(random.uniform(3, 6))
                plan = choose(cands) if cands else []
                res: dict[str, Any] = {"keyword": ", ".join(keywords), "seen": len(cards), "candidates": len(cands), "planned": len(plan),
                                       "posted": 0, "failed": 0, "plan": [(c["author"], c["reactions"], t) for c, t in plan]}
                if dry_run:
                    return res
                on = keywords[-1]  # the page currently open
                for c, text in plan[:limit]:
                    try:
                        if c.get("kw") != on:
                            await _search(page, c["kw"])
                            on = c["kw"]
                        ok = await _post_comment(page, c, text)
                    except Exception as e:  # one broken post must not stop the rest
                        ok = False
                        res["error"] = str(e)[:160]
                    on_posted(c, text, ok)
                    res["posted" if ok else "failed"] += 1
                    if res["failed"] >= 2:
                        break
                    await asyncio.sleep(random.uniform(60, 150))
                return res
            finally:
                await ctx.close()
