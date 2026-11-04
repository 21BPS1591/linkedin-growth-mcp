"""Pre-publish safety gates for unattended posting. Every check is deterministic (no LLM judgement)."""
from __future__ import annotations

import re

NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
PCT = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(?:%|per ?cent|percent|pp\b)", re.IGNORECASE)
MULT = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(?:x\b|times\b)", re.IGNORECASE)
FORBIDDEN = re.compile(
    r"\b(subscribe|avoid this|must[- ]apply|buy|sell|(?:should|must|can|could|would|to|and|or|just|then) hold|hold (?:it|this|the (?:stock|shares?|ipo)|for|on)|target price|multibagger|guaranteed|sure[- ]shot|"
    r"can't go wrong|do not miss|don't miss|best ipo|worst ipo|apply for|skip this)\b", re.IGNORECASE)
LINK_OR_MENTION = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|in|io|co|net|org)\b|(?<!\w)@\w", re.IGNORECASE)
DISCLAIMER = re.compile(r"not (an )?(investment|financial) advice", re.IGNORECASE)

_YEAR_LINE = re.compile(r"^(?P<year>(?:FY\s*)?\d{2,4}(?:\s*[-–/]\s*\d{2,4})?):\s*revenue\s+(?P<rev>-?[\d.]+|None)\s*cr",
                        re.IGNORECASE)
_METRICS = {"revenue": r"revenue\s+(-?[\d.]+)\s*cr", "ebitda": r"EBITDA\s+(-?[\d.]+)\s*cr", "pat": r"PAT\s+(-?[\d.]+)\s*cr"}


def _floats(text: str) -> list[float]:
    out = []
    for tok in NUM.findall(text):
        try:
            out.append(float(tok.replace(",", "")))
        except ValueError:
            pass
    return out


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _near(v: float, pool: list[float], rel: float = 0.008, absolute: float = 0.51) -> bool:
    """Match allowing the rounding a writer would apply (803.629 -> 804, 21.25 -> 21.3)."""
    return any(abs(v - p) <= max(0.0051, abs(p) * rel) or (float(v).is_integer() and abs(v - p) <= absolute) for p in pool)


def yearly_metrics(facts: str) -> dict[str, dict[str, float]]:
    """{'revenue': {'FY 2024': 657.5, ...}, 'pat': {...}} parsed from the FACTS lines."""
    out: dict[str, dict[str, float]] = {k: {} for k in _METRICS}
    for line in facts.splitlines():
        m = _YEAR_LINE.match(line.strip())
        if not m:
            continue
        year = m.group("year").strip()
        for key, rx in _METRICS.items():
            mm = re.search(rx, line, re.IGNORECASE)
            if mm:
                try:
                    out[key][year] = float(mm.group(1))
                except ValueError:
                    pass
    return out


def derived_percents(facts: str) -> list[float]:
    """Percentages a writer may legitimately compute: YoY growth, total growth, CAGR and margins."""
    ym = yearly_metrics(facts)
    out: list[float] = []
    for key, series in ym.items():
        years = sorted(series, key=lambda y: re.sub(r"\D", "", y))
        vals = [series[y] for y in years]
        for a, b in zip(vals, vals[1:]):
            if a:
                out.append(100 * (b / a - 1))
        if len(vals) >= 2 and vals[0] > 0 and vals[-1] > 0:
            out.append(100 * (vals[-1] / vals[0] - 1))
            out.append(100 * ((vals[-1] / vals[0]) ** (1 / (len(vals) - 1)) - 1))
    for num in ("pat", "ebitda"):
        for y, v in ym[num].items():
            r = ym["revenue"].get(y)
            if r:
                out.append(100 * v / r)
    return [abs(x) for x in out]


def derived_multiples(facts: str) -> list[float]:
    """'N times larger' figures: any pair of revenue figures (IPO company vs its one peer)."""
    revs = [_num(m) for m in re.findall(r"revenue\s+(-?[\d.,]+)\s*cr", facts, re.IGNORECASE) if m not in ("", ".")]
    return [a / b for a in revs for b in revs if b and a != b]


def ungrounded_numbers(text: str, facts: str) -> list[float]:
    """Numbers in the post that are not in FACTS (percentages and multiples may also be computed from FACTS)."""
    base = sorted({abs(v) for v in _floats(facts)})
    pct_pool = base + derived_percents(facts)
    mult_pool = derived_multiples(facts)
    pct_vals = {_num(m) for m in PCT.findall(text)}
    mult_vals = {_num(m) for m in MULT.findall(text)}
    bad = []
    for v in _floats(text):
        if v <= 12 and float(v).is_integer():  # list numbering, 'top 3', month/day
            continue
        if 1990 <= v <= 2100 and float(v).is_integer():  # years
            continue
        if v in pct_vals:  # a percentage: stated in FACTS or computed from FACTS, to the writer's rounding
            slack = 0.51 if float(v).is_integer() else 0.06
            ok = any(abs(v - p) <= slack for p in pct_pool)
        elif v in mult_vals:
            ok = any(abs(v - p) <= 0.06 + 0.02 * v for p in mult_pool) or _near(v, base)
        else:
            ok = _near(v, base)
        if not ok:
            bad.append(v)
    return bad


HASHTAG = re.compile(r"(?<!\w)#\w+")
BARE_OPEN = re.compile(r"^[^\w₹$]{0,3}[₹$]?\s?\d")
MANY_DECIMALS = re.compile(r"(?<![\w.])\d[\d,]*\.\d{3,}")
PROCEEDS_CLAIM = re.compile(
    r"primary objective|main objective|funding (?:its |their |the )?(?:future|expansion|growth)|future operations|"
    r"working capital|repay(?:ing|ment of)? (?:its |their )?debt|general corporate purposes|"
    r"(?:proceeds|funds) (?:will|would|to) be (?:used|utili[sz]ed)", re.IGNORECASE)


_COMPUTED_FIG = re.compile(r"(\d[\d,]*\.?\d*)(?=\s*(?:%|x\b))")


def required_comparison_figures(facts: str) -> list[float]:
    """Figures from the 'Computed:' lines (growth, margins, P/E, scale) that the post must actually show."""
    out: list[float] = []
    for line in facts.splitlines():
        if line.startswith("Computed:"):
            out += [float(m.replace(",", "")) for m in _COMPUTED_FIG.findall(line)]
    return out


def comparison_shown(text: str, facts: str, need: int = 3) -> bool:
    req = required_comparison_figures(facts)
    if len(req) < need:
        return True  # not enough computed figures to demand any
    have = _floats(text)
    shown = sum(1 for r in req if any(abs(v - r) <= (0.51 if float(v).is_integer() else 0.06) for v in have))
    return shown >= need


def style_problems(text: str, facts: str) -> list[str]:
    """Readability and sourcing rules a cheap model tends to break."""
    out: list[str] = []
    first = next((ln for ln in text.strip().splitlines() if ln.strip()), "")
    if BARE_OPEN.match(first):
        out.append("opens with a bare number: start with a plain-English sentence about the business")
    if MANY_DECIMALS.search(text):
        out.append("uses numbers with 3+ decimals: round to whole crore / one decimal")
    if len(HASHTAG.findall(text)) > 3:
        out.append("more than 3 hashtags")
    if not comparison_shown(text, facts):
        out.append("does not show the side-by-side comparison numbers: include the 'Computed:' figures "
                   "(growth, margins, P/E, scale) as short comparison lines")
    if "Offer for sale:" in facts and not re.search(r"(?i)(existing|selling) (share|stock)holders?|selling shareholder", text):
        out.append("does not say that the offer-for-sale money goes to the selling shareholders, not the company")
    if "Peer choice:" in facts and not re.search(r"(?i)closest|nearest", text):
        out.append("does not say why this peer was chosen (it is the closest in revenue among the peers the RHP lists)")
    if "Object:" not in facts and PROCEEDS_CLAIM.search(text):
        out.append("says what the IPO money will be used for, which is not in the verified facts: remove it")
    return out


def check_post(text: str, facts: str, series: str, banned_terms: list[str] | None = None) -> list[str]:
    """Return the reasons the post must not be auto-published (empty list == all gates pass)."""
    fails: list[str] = []
    bad_terms = banned_term_hits(text, banned_terms)
    if bad_terms:
        fails.append(f"mentions off-limits terms: {bad_terms[:6]}")
    fails += style_problems(text, facts)
    if "[TBD" in text:
        fails.append("contains [TBD] placeholders")
    if not 350 <= len(text) <= 3000:
        fails.append(f"length {len(text)} outside 350-3000 characters")
    if LINK_OR_MENTION.search(text):
        fails.append("contains a link or @mention")
    hit = FORBIDDEN.search(text)
    if hit:
        fails.append(f"advice-like wording: '{hit.group(0)}'")
    if series == "ipo" and not DISCLAIMER.search(text):
        fails.append("missing 'not investment advice' disclaimer")
    if not facts.strip():
        fails.append("no verified facts attached")
    else:
        bad = ungrounded_numbers(text, facts)
        if bad:
            fails.append(f"numbers not found in verified facts: {bad[:6]}")
    return fails


def banned_term_hits(text: str, terms: list[str] | None) -> list[str]:
    """Owner's off-limits terms (employer, vendors, client names, colleagues...) found in the text.
    Case-insensitive, whole-word match; zero model calls."""
    hits: list[str] = []
    for t in terms or []:
        t = str(t).strip()
        if len(t) < 2:
            continue
        if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", text, re.IGNORECASE):
            hits.append(t)
    return hits


def check_general(text: str, banned_terms: list[str] | None = None, facts: str = "") -> list[str]:
    """Gates for non-IPO posts: no verified-facts pack exists, so only form, safety and wording are checked."""
    fails: list[str] = []
    bad_terms = banned_term_hits(text, banned_terms)
    if bad_terms:
        fails.append(f"mentions off-limits terms: {bad_terms[:6]}")
    if "[TBD" in text:
        fails.append("contains [TBD] placeholders")
    if not 350 <= len(text) <= 3000:
        fails.append(f"length {len(text)} outside 350-3000 characters")
    if LINK_OR_MENTION.search(text):
        fails.append("contains a link or @mention")
    hit = FORBIDDEN.search(text)
    if hit:
        fails.append(f"advice-like wording: '{hit.group(0)}'")
    first = next((ln for ln in text.strip().splitlines() if ln.strip()), "")
    if BARE_OPEN.match(first):
        # number-led opener is a tested arm for non-IPO posts: only "<count> <thing>" and only with a story bank / facts
        # attached (the numbers gate below then confirms the figure is sourced). Currency or bare figures stay banned.
        if not re.match(r"^\s*[\d,]+\s+[A-Za-z]", first) or not facts.strip():
            fails.append("opens with a bare number or currency (allowed only as '<count> <thing>' sourced from the story bank)")
    if len(HASHTAG.findall(text)) > 3:
        fails.append("more than 3 hashtags")
    if re.search(r"[\u2013\u2014]", text):
        fails.append("contains an em or en dash")
    if facts.strip():  # owner's story bank attached: every figure must come from it
        bad = ungrounded_numbers(text, facts)
        if bad:
            fails.append(f"numbers not found in the story bank: {bad[:6]}")
    return fails
