"""Catalog of post styles and hook types; each (style, hook) pair is a bandit arm."""
from __future__ import annotations

from itertools import product

STYLES: dict[str, str] = {
    "story": "A short first-person story with a concrete moment, a turning point, and one lesson.",
    "listicle": "A numbered list of 3-7 punchy, specific points, each one line.",
    "contrarian": "Challenge a common belief in the field, then back the claim with reasoning or an example.",
    "how_to": "Step-by-step practical guide the reader can apply today.",
    "question": "Open with a genuine question to the audience, share a brief take, invite replies.",
    "case_study": "Describe a real result: context, what was done, numbers, and takeaways.",
}

HOOKS: dict[str, str] = {
    "bold_claim": "Open with a bold, specific claim.",
    "number": "Open with a specific count from the FACTS followed by the thing it counts (e.g. '1,000 manual approvals a month ...'). Never invent or round the number; if FACTS has none, open with plain English instead.",
    "question": "Open with a direct question.",
    "confession": "Open with a candid admission or mistake.",
}


def all_arms() -> list[tuple[str, str]]:
    return list(product(STYLES, HOOKS))


def arm_key(style: str, hook: str) -> str:
    return f"{style}|{hook}"
