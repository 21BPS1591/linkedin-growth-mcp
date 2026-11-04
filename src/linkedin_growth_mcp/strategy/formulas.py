"""Structure hints adapted from linkedin-skills F-formulas. They are HYPOTHESES, not rules: the reach claims behind
them are vendor data and some conflict with our own results, so they only shape structure and never the opener
(the opener stays with the style x hook bandit and the hook linter)."""
from __future__ import annotations

# style -> rotating structure hints (formula id, instruction)
HINTS: dict[str, list[tuple[str, str]]] = {
    "story": [
        ("F4", "Anchor the story in time ('three weeks ago', 'last Tuesday') and admit the specific thing that went wrong."),
        ("F17", "Compare exactly two versions of the same task that differ in ONE variable, and say what changed in the outcome."),
        ("F19", "Pair one concrete moment with one piece of evidence from the facts, then state what you now do differently."),
    ],
    "contrarian": [
        ("F10", "State the common belief, give the history or reason it became common, then show where it breaks."),
        ("F18", "Show that the either/or everyone argues about is a false choice, and name what actually decides it."),
    ],
    "how_to": [
        ("F15", "Explain it as you would to a smart newcomer: plain words, one analogy, then the steps."),
        ("F17", "Show the same task done two ways that differ in one variable, then give the steps of the better way."),
    ],
    "listicle": [("F12", "Give the reader permission to stop doing something, then list the few things worth keeping.")],
    "question": [("F9", "Raise a gap in the first lines and pay it off by the third paragraph, then ask one narrow closing question.")],
    "case_study": [("F19", "Context, what was done, one number from the facts, and the one change you would make next time.")],
}


def formula_hint(style: str, n_posts: int) -> str:
    opts = HINTS.get(style) or []
    if not opts:
        return ""
    fid, text = opts[n_posts % len(opts)]
    return f"STRUCTURE HINT ({fid}, a suggestion only, never copy wording or change the opening): {text}\n"
