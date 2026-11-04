"""Follower count from the owner's own profile page. One number a day: the real measure of account growth."""
from __future__ import annotations

import re

from .browser import LinkedInBrowser

_FOLLOWERS = re.compile(r"([\d,]+(?:\.\d+)?\s*[KkMm]?)\s*followers?\b", re.IGNORECASE)


def parse_followers(text: str) -> int | None:
    """First 'N followers' figure on the page, or None. LinkedIn abbreviates large counts (1.2K).

    The first one is the profile header's own count; later ones belong to other accounts the page suggests (a
    'largest wins' rule once stored 12.4M followers from a company card)."""
    for raw in _FOLLOWERS.findall(text):
        s = raw.strip().replace(",", "")
        mult = 1
        if s[-1] in "Kk":
            mult, s = 1_000, s[:-1]
        elif s[-1] in "Mm":
            mult, s = 1_000_000, s[:-1]
        try:
            return int(float(s.strip()) * mult)
        except ValueError:
            continue
    return None


async def scrape_followers(browser: LinkedInBrowser) -> int | None:
    return parse_followers(await browser.read_text("https://www.linkedin.com/in/me/", scroll=3))
