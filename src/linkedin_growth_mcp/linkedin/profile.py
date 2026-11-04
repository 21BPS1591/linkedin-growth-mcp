"""Own-profile reader. Returns raw visible text; Gemini interprets it, so DOM changes don't break us."""
from __future__ import annotations

import time
from typing import Any

from .browser import LinkedInBrowser

MAX_CHARS = 12000


async def scrape_own_profile(browser: LinkedInBrowser) -> dict[str, Any]:
    text = await browser.read_text("https://www.linkedin.com/in/me/", scroll=5)
    return {"source": "linkedin_in_me", "captured_at": time.time(), "raw_text": text[:MAX_CHARS]}
