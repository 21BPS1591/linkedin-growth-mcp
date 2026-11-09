"""Hook analysis: lint the opening of a post against the rule book and learn which opening patterns earn reach.

Pure functions only. Nothing here changes what the autopilot posts.
"""
from __future__ import annotations

import re
from statistics import mean
from typing import Any

from .scoring import external_reactions

FEED_CUT = 210  # characters the feed shows before "see more"
DASHES = ("—", "–")
_LINK = re.compile(r"https?://|www\.|(?<!\w)@\w|#\w", re.IGNORECASE)
_BARE_LEAD = re.compile(r"^\s*(?:[₹$€£]|rs\.?\s*|inr\s*)?\d")  # a number or currency as the very first token
_FILLER = re.compile(
    r"^\s*(?:i['’]?m (?:excited|thrilled|happy|proud)|excited to|thrilled to|delighted to|in today['’]?s|"
    r"hello (?:everyone|connections)|hi (?:everyone|connections)|here['’]?s (?:a|my|the)|let me tell you)",
    re.IGNORECASE)
_CONTRARIAN = re.compile(r"\b(?:most people|nobody|no one|stop |myth|wrong|overrated|unpopular|actually|truth about|isn['’]?t)\b",
                         re.IGNORECASE)
_CONFESSION = re.compile(r"\b(?:i (?:was wrong|made a mistake|failed|lost|missed|got it wrong|almost)|my (?:mistake|biggest mistake)|"
                         r"i used to|i thought|confession)\b", re.IGNORECASE)
_STORY = re.compile(r"^\s*(?:last (?:week|month|year|night)|yesterday|when i|two (?:years|weeks|days)|a (?:friend|client|manager)|"
                    r"it was|on (?:monday|tuesday|wednesday|thursday|friday))", re.IGNORECASE)
_TENSION = re.compile(r"\b(?:but|yet|until|instead|without|vs\.?|versus|why|how|what|never|only|still)\b", re.IGNORECASE)


def hook_text(post: str) -> str:
    """What the feed shows before 'see more': the first 210 characters, cut at a word."""
    text = " ".join(post.split())
    if len(text) <= FEED_CUT:
        return text
    cut = text[:FEED_CUT]
    return cut[: cut.rfind(" ")] if " " in cut else cut


def classify_hook(post: str) -> str:
    """Opening pattern of the first line. Used to group past posts, not to judge quality."""
    first = (post.strip().splitlines() or [""])[0]
    if _BARE_LEAD.match(first):
        return "number_led"
    if "?" in first[:FEED_CUT]:
        return "question"
    if _CONFESSION.search(first):
        return "confession"
    if _STORY.match(first):
        return "story"
    if _CONTRARIAN.search(first):
        return "contrarian"
    return "statement"


def lint_hook(post: str) -> dict[str, Any]:
    """Deterministic check of the first 210 characters against the rule book (B1). Score 0 to 100 plus plain reasons."""
    hook = hook_text(post)
    first = (post.strip().splitlines() or [""])[0].strip()
    issues: list[str] = []
    score = 100
    if not hook:
        return {"score": 0, "pattern": "statement", "hook": "", "issues": ["empty post"], "fixes_needed": True}
    if _BARE_LEAD.match(first):
        issues.append("Opens with a bare number or currency. Lead with plain English, then the number.")
        score -= 25
    if _FILLER.match(first):
        issues.append("Opens with filler ('excited to share', 'in today's'...). Say the point instead.")
        score -= 30
    if any(d in hook for d in DASHES):
        issues.append("Contains an em or en dash. The rule book bans them.")
        score -= 20
    if _LINK.search(hook):
        issues.append("Link, @mention or hashtag inside the hook. Keep those out of the opening.")
        score -= 20
    if len(first) > 150:
        issues.append(f"First line is {len(first)} characters. Aim for 150 or less so it reads in one glance.")
        score -= 15
    if len(first) < 25:
        issues.append("First line is very short; it may not give a reason to click 'see more'.")
        score -= 10
    if max((len(w) for w in re.findall(r"[A-Za-z]+", first)), default=0) > 18:
        issues.append("Contains a very long word or jargon token in the first line.")
        score -= 5
    if not ("?" in hook or _TENSION.search(hook) or re.search(r"\d", hook)):
        issues.append("No tension: no question, contrast or concrete number. Add what the reader will find out.")
        score -= 15
    return {"score": max(0, score), "pattern": classify_hook(post), "hook": hook, "issues": issues, "fixes_needed": score < 80}


def hook_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Group past posts by opening pattern. rows: {text, impressions, reactions, comments, reposts}.

    Reports averages and sample sizes, and refuses to name a winner until each side has enough posts."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(classify_hook(r["text"]), []).append(r)
    out = []
    for pattern, items in groups.items():
        out.append({
            "pattern": pattern, "posts": len(items),
            "avg_impressions": round(mean(i["impressions"] for i in items)),
            "avg_reactions": round(mean(external_reactions(i["reactions"]) for i in items), 1),
            "avg_comments": round(mean(i["comments"] for i in items), 2),
        })
    out.sort(key=lambda g: g["avg_impressions"], reverse=True)
    enough = [g for g in out if g["posts"] >= 5]
    if len(enough) >= 2:
        verdict = f"'{enough[0]['pattern']}' leads on average impressions over {enough[0]['posts']} posts. Still treat it as a hint."
    else:
        verdict = ("Not enough data to name a winning hook: a pattern needs at least 5 posts and two patterns must be "
                   "compared. Any ranking below is noise at this size.")
    return {"posts": len(rows), "by_pattern": out, "verdict": verdict}
