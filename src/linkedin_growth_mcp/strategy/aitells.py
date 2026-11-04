"""AI-tell lint: pure regex, zero model calls. Soft score only (logged, never blocks a post).

Patterns ported from linkedin-skills `audit-ai-tells.md`. Dashes are left out because
`strip_dashes` already removes them. Also checks rule B3 (one specific closing question).
"""
from __future__ import annotations

import re
from typing import Any

_VERBS = ("leverag", "utiliz", "facilitat", "streamlin", "delv", "navigat",
          "unlock", "harness", "foster", "cultivat", "elevat", "empower")

# Counted per paragraph: 3+ markers = rewrite, 2 = replace the weakest.
DENSITY = {
    "vocab_verbs": re.compile(r"(?i)\b(?:" + "|".join(_VERBS) + r")(?:e|es|ed|ing|s)?\b"),
    "vocab_2026": re.compile(r"(?i)\b(significant(ly)?|crucial(ly)?|notably|particularly|comprehensive|insights?|robust|landscape|nuanced|multifaceted|holistic|seamless|ecosystem)\b"),
    "adverb_filler": re.compile(r"(?i)\b(fundamentally|essentially|ultimately|arguably|certainly|definitely|undoubtedly)\b"),
    "ing_opener": re.compile(r"(?m)^[\s>*\-]*[A-Z][a-z]+ing\b[^.\n]{0,60},"),
    "nominalisation": re.compile(r"(?i)\bthe \w+(?:tion|sion|ment|ance|ence|ization|isation) of\b"),
    "linkedin_2026": re.compile(r"(?i)\b(quietly|compound(s|ing)?|(a|the) signal|the work|built different|load-bearing|doing the heavy lifting)\b"),
    "decaying_2024": re.compile(r"(?i)\b(delve|delving|tapestry|realm|intricate|journey|paradigm)\b"),
}

# One hit = one flag.
SINGLE = {
    "reveal_bridge": re.compile(r"(?im)^(the (result|outcome|answer|lesson|catch|kicker|truth)\?|here'?s (what|how|why|the thing)\b|stop \w+[^.\n]{0,40}[.,] ?start \b|plot twist:)"),
    "inflated_symbolism": re.compile(r"(?i)not just \w+, it'?s \w+"),
    "neg_parallel": re.compile(r"(?i)\b(isn'?t|not) (about )?[^,.\n]{1,40}, it'?s (about )?\b"),
    "staccato_stack": re.compile(r"(?m)^(\w+\. ){2,}\w+\.$"),
    "one_word_paragraph": re.compile(r"(?m)^\w+\.$"),
    "no_no_just": re.compile(r"(?i)\bno \w+\. no \w+\. (just|only) \w+"),
    "pseudo_socratic": re.compile(r"(?i)\b(why|how)\? (because|simple)\b"),
    "sincerity_marker": re.compile(r"(?im)^[\s>*\-]*(let me be (honest|real|direct|clear)|i'?ll be (honest|real|direct)|honestly\?|the honest (version|answer|truth) is|to be (direct|honest|transparent)|real talk|full transparency|not gonna lie|ngl|unpopular opinion)\b"),
    "opener_filler": re.compile(r"(?im)^[\s>*\-]*[\"'“]?(In today's|Have you ever|Most people don't realize|Here's a hard truth)"),
    "closer_filler": re.compile(r"(?i)(what (do|are) you (think|your? thought)|what(?:'s| is) your (take|thoughts?)|thoughts\?|agree or disagree\?|let me know in the comments|tag someone|let that sink in|that'?s the real story)"),
}

_HASHTAG_LINE = re.compile(r"^(\s*#\w+)+\s*$")


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip() and not _HASHTAG_LINE.match(p.strip())]


def lint_ai_tells(text: str) -> dict[str, Any]:
    """Score 0-100 (100 = no tells) plus the reasons. Never raises, never blocks."""
    flags: list[str] = []
    for name, rx in SINGLE.items():
        if rx.search(text):
            flags.append(name)
    dense = 0
    for para in _paragraphs(text):
        n = sum(len(rx.findall(para)) for rx in DENSITY.values())
        if n >= 3:
            dense += 1
            flags.append(f"dense_paragraph({n} markers): {para[:40]}...")
    paras = _paragraphs(text)
    last = paras[-1] if paras else ""
    closing_question = "?" in last
    if not closing_question:
        flags.append("no_closing_question")  # rule B3
    penalty = 12 * sum(1 for f in flags if f in SINGLE) + 18 * dense + (8 if not closing_question else 0)
    return {"score": max(0, 100 - penalty), "flags": flags, "closing_question": closing_question}
