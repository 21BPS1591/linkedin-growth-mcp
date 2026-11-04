"""Deterministic helpers (no model calls): story interview -> idea, voice fingerprint, profile scorecard."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

# 5-line spine from the linkedin-skills interviewer (post mode). One question at a time; empty answers stay empty.
SPINE_QUESTIONS = [
    ("moment", "Moment: what exactly happened, when, and where? One concrete scene."),
    ("number", "Number: what is the one figure that proves it (mark it 'est.' if it is an estimate)?"),
    ("correction", "Correction: what did you believe or do before that turned out wrong?"),
    ("opposition", "Opposition: what do most people in your field do instead, and what does your way cost you?"),
    ("ask", "Ask: what one specific question do you want readers to answer?"),
]


def interview_to_idea(title: str, answers: dict[str, str]) -> tuple[str, str]:
    """Build (brief, facts) from spine answers. Only the owner's own words go into facts; empty lines stay out."""
    a = {k: (answers.get(k) or "").strip() for k, _ in SPINE_QUESTIONS}
    brief = "\n".join(f"{k.capitalize()}: {v or '(not given)'}" for k, v in a.items())
    brief += "\nWrite it in first person from these answers only. End with the Ask as the closing question."
    facts = "\n".join(f"{k.capitalize()}: {v}" for k, v in a.items() if v and k != "ask")
    return brief, facts


def voice_fingerprint(samples: list[str], notes: str = "") -> dict[str, Any]:
    """Structured fingerprint of text the owner typed themselves."""
    text = "\n\n".join(s.strip() for s in samples if s and s.strip())
    if not text:
        return {}
    sents = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    words = re.findall(r"[A-Za-z']+", text)
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    low = [w.lower() for w in words]
    firsts = Counter(" ".join(s.split()[:2]).lower() for s in sents if len(s.split()) >= 2)
    avg_words = round(len(words) / max(1, len(sents)), 1)
    return {
        "notes": notes.strip(),
        "unpunctuated": avg_words > 40,  # run-on samples: sentence-length stats would mislead the model
        "samples": len(samples),
        "avg_sentence_words": round(len(words) / max(1, len(sents)), 1),
        "avg_paragraph_sentences": round(len(sents) / max(1, len(paras)), 1),
        "question_rate": round(sum(1 for s in sents if s.rstrip().endswith("?")) / max(1, len(sents)), 2),
        "first_person_rate": round(sum(1 for w in low if w in {"i", "i'm", "i've", "my", "me"}) / max(1, len(low)), 3),
        "contraction_rate": round(sum(1 for w in words if "'" in w) / max(1, len(words)), 3),
        "common_openers": [o for o, c in firsts.most_common(4) if c > 1],
        "excerpts": [s.strip()[:300] for s in samples if s and s.strip()][:3],
    }


def voice_block(fp: dict[str, Any]) -> str:
    """Prompt text for the drafting model."""
    if not fp:
        return ""
    stats = "" if fp.get("unpunctuated") else (
        f"sentences average {fp['avg_sentence_words']} words, paragraphs {fp['avg_paragraph_sentences']} sentences, "
        f"questions {int(fp['question_rate'] * 100)}% of sentences, contractions {'common' if fp['contraction_rate'] > 0.02 else 'rare'}. ")
    return (
        "OWNER'S VOICE (from text they typed themselves; match the rhythm, not the words): " + stats
        + (fp.get("notes", "") + " " if fp.get("notes") else "")
        + "Write with correct punctuation and short sentences. Do NOT copy typos, run-ons, slang spellings or exact phrases. Excerpts:\n"
        + "\n".join(f"- {e}" for e in fp.get("excerpts", [])) + "\n\n"
    )


def profile_scorecard(raw_text: str) -> dict[str, Any]:
    """Rule-based grade of the scraped profile text. Sections that raw text cannot reveal say 'unknown'."""
    text = raw_text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    low = text.lower()
    rows: list[dict[str, str]] = []

    def row(name: str, status: str, note: str) -> None:
        rows.append({"section": name, "status": status, "note": note})

    head = next((ln for ln in lines[:12] if 25 <= len(ln) <= 260 and "|" in ln or 40 <= len(ln) <= 220), "")
    if not head:
        row("Headline", "fail", "no headline-like line found near the top")
    elif len(head) > 220:
        row("Headline", "needs_work", f"{len(head)} chars, limit 220")
    elif "|" in head or re.search(r"\b(help|build|turn|for)\b", head, re.I):
        row("Headline", "pass", f"{len(head)} chars, names what you do or who you help")
    else:
        row("Headline", "needs_work", "add who you help and the result: What You Do | Who You Help | Result")

    m = re.search(r"\babout\b\s*\n(.{50,2600}?)(?:\n(?:featured|activity|experience|education)\b|$)", text, re.I | re.S)
    if m:
        about = m.group(1).strip()
        words = len(about.split())
        hook_ok = len(about.split("\n")[0]) <= 275
        row("About", "pass" if 200 <= words <= 300 and hook_ok else "needs_work",
            f"{words} words (aim 200-300); first line {'fits' if hook_ok else 'is too long for'} the ~275 char fold")
    else:
        row("About", "fail", "no About section found")

    row("Featured", "pass" if "featured" in low else "needs_work", "section present" if "featured" in low else "no Featured section; pin 3 items matched to your goal")
    row("Experience", "pass" if re.search(r"\d+\s?%|\d[\d,]*\s+(hours|people|users|reports|approvals)", low) else "needs_work",
        "has quantified results" if re.search(r"\d+\s?%|\d[\d,]*\s+(hours|people|users|reports|approvals)", low) else "no metric found; use action verb + number")
    sk = re.search(r"(\d+)\s+skills?", low)
    row("Skills", "unknown" if not sk else ("pass" if int(sk.group(1)) >= 5 else "needs_work"), "skill count read from page" if sk else "count not visible in text")
    rec = re.search(r"recommendations?\s*\(?(\d+)", low)
    row("Recommendations", "unknown" if not rec else ("pass" if int(rec.group(1)) >= 3 else "needs_work"), "aim for 3+ specific ones" if rec else "count not visible in text")
    row("Photo, banner, custom URL", "unknown", "cannot be read from page text; check by eye (banner 1584x396, URL without number tail)")

    graded = [r for r in rows if r["status"] != "unknown"]
    pts = sum({"pass": 1, "needs_work": 0.5, "fail": 0}[r["status"]] for r in graded)
    return {"score": round(100 * pts / max(1, len(graded))), "graded": len(graded), "rows": rows}
