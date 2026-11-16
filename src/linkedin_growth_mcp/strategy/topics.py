"""Topic mix: which kind of non-IPO post the day should be, plus validation of auto-generated backlog ideas.

Pure functions, no model calls. The weekly mix (5 weekdays): 2 fresher career posts, 2 work stories, 1 tools (BI or AI).
Set from the first 14 posts (Nov 2026): career averaged about 400 impressions, story about 300, tools about 240.
IPO posts are chosen elsewhere (quality ranking, weekly cap, IPO_POSTS switch); this module decides among the rest.
"""
from __future__ import annotations

import re
from typing import Any

GROUP_OF = {"ipo": "ipo", "bi_features": "tools", "ai": "tools", "product": "tools", "story": "story", "career": "career"}
TARGET = {"ipo": 0.0, "tools": 1.0, "story": 2.0, "career": 2.0}  # posts per 7 days
FLOOR_DAYS = 30  # every group is tried at least this often, whatever the results say
PERF_WEIGHT = 0.5


def rank_groups(counts_7d: dict[str, int], perf: dict[str, float], days_since: dict[str, float], allowed: set[str]) -> list[str]:
    """Allowed groups, most-deserving first. Deficit against the weekly mix leads; past results nudge (weight 0.5);
    a group not posted for FLOOR_DAYS gets a boost so a weak early result can never bury a topic."""
    groups = {GROUP_OF[s] for s in allowed if s in GROUP_OF} - {"ipo"}  # IPO posts have their own path
    if perf:
        lo, hi = min(perf.values()), max(perf.values())
    else:
        lo = hi = 0.0

    def norm(g: str) -> float:
        return 0.5 if g not in perf or hi == lo else (perf[g] - lo) / (hi - lo)

    def score(g: str) -> float:
        deficit = TARGET[g] - counts_7d.get(g, 0)
        floor = 1.0 if days_since.get(g, 999.0) >= FLOOR_DAYS else 0.0
        return deficit + PERF_WEIGHT * norm(g) + floor

    return sorted(groups, key=lambda g: (-score(g), g))


def clean_ideas(raw: Any, allowed: set[str], existing_titles: set[str], banned_hit, limit: int = 8) -> list[dict[str, str]]:
    """Validate model-proposed ideas: allowed series, unique title, no dashes, no banned term, sane length."""
    items = raw.get("ideas") if isinstance(raw, dict) else raw
    out: list[dict[str, str]] = []
    seen = {t.lower() for t in existing_titles}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        series = str(it.get("series", "")).strip()
        title = re.sub(r"[\u2013\u2014]", ",", " ".join(str(it.get("title", "")).split()))[:120]
        brief = re.sub(r"[\u2013\u2014]", ",", " ".join(str(it.get("brief", "")).split()))[:600]
        if series not in allowed or series == "ipo" or len(title) < 12 or len(brief) < 20 or title.lower() in seen:
            continue
        if banned_hit(title + " " + brief) or re.search(r"https?://|@\w", title + brief):
            continue
        seen.add(title.lower())
        out.append({"series": series, "title": title, "brief": brief})
        if len(out) >= limit:
            break
    return out
