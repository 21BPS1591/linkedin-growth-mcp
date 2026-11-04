"""Pick hashtags per post from its own content: topic match first, then a broad / mid / niche mix."""
from __future__ import annotations

import re

# tag -> (tier, topic keywords). Tiers: 0 broad, 1 mid, 2 niche.
POOL: dict[str, tuple[int, tuple[str, ...]]] = {
    "#Automation": (0, ("automat", "manual", "workflow", "repetitive")),
    "#DataAnalytics": (1, ("data", "dashboard", "analy", "metric", "report")),
    "#FinanceTransformation": (2, ("finance", "expense", "approval", "accounting", "audit")),
    "#ProcessImprovement": (2, ("process", "bottleneck", "workflow", "error", "system")),
    "#PowerBI": (1, ("power bi", "powerbi", "dax")),
    "#SQL": (1, ("sql", "query", "database")),
    "#Python": (1, ("python", "script", "pandas")),
    "#GenAI": (1, ("gemini", "llm", "gpt", "claude", "generative")),
    "#DataEngineering": (2, ("pipeline", "etl", "warehouse", "sync")),
    "#Excel": (1, ("excel", "spreadsheet")),
    "#CareerGrowth": (0, ("career", "promotion", "interview", "job", "learned")),
    "#Leadership": (0, ("lead", "team", "manager", "stakeholder")),
    "#DataStorytelling": (2, ("story", "insight", "visual", "present")),
}
_TAG = re.compile(r"(?<!\w)#\w+")


MIN_POSTS = 3  # a tag needs this many measured posts before its results move the ranking


def pick_hashtags(text: str, limit: int = 4, stats: dict[str, tuple[int, float]] | None = None) -> list[str]:
    """Score each pool tag by keyword hits (scaled by past reach once measured), then fill one broad, up to two mid and up to two niche slots."""
    low = _TAG.sub("", text).lower()
    stats = stats or {}
    measured = [avg for n, avg in stats.values() if n >= MIN_POSTS]
    overall = sum(measured) / len(measured) if measured else 0
    scored = []
    for tag, (tier, words) in POOL.items():
        hits = sum(low.count(w) for w in words)
        if hits:
            n, avg = stats.get(tag, (0, 0.0))
            if n >= MIN_POSTS and overall:
                hits *= min(max(avg / overall, 0.5), 2.0)  # proven tags up to 2x, weak ones down to 0.5x
            scored.append((hits, tag, tier))
    scored.sort(key=lambda s: (-s[0], s[2]))
    caps = {0: 1, 1: 2, 2: 2}
    used = {0: 0, 1: 0, 2: 0}
    out: list[str] = []
    for _, tag, tier in scored:
        if used[tier] < caps[tier] and len(out) < limit:
            out.append(tag)
            used[tier] += 1
    return out
