"""Turn raw metrics into a comparable score in [0, 1]."""
from __future__ import annotations

import math


def engagement_per_1k(impressions: int, reactions: int, comments: int, reposts: int) -> float:
    """Weighted engagement per 1000 impressions (comments and reposts weigh more than reactions)."""
    if impressions <= 0:
        return 0.0
    weighted = reactions + 3 * comments + 5 * reposts
    return 1000.0 * weighted / impressions


def post_score(impressions: int, reactions: int, comments: int, reposts: int, baseline_impressions: float) -> float:
    """Blend of reach (vs. the account's running baseline) and engagement rate.

    Both terms are squashed to [0, 1]; reach gets 60% because the stated goal is growing impressions.
    """
    reach = impressions / (impressions + max(baseline_impressions, 1.0))  # 0.5 == baseline
    eng = engagement_per_1k(impressions, reactions, comments, reposts)
    eng_norm = 1.0 - math.exp(-eng / 50.0)  # ~63% of max at 50 weighted engagements/1k
    return 0.6 * reach + 0.4 * eng_norm
