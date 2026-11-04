"""RHP (Red Herring Prospectus) section finder and Gemini extractor.

RHPs run 300-500 pages, so we locate the handful of pages that matter, send only those to Gemini as a
small PDF, and ask for every figure with its ORIGINAL page number so a human can verify it.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

# section -> (heading regex, max pages to take, first page index where the real section can start)
SECTIONS: dict[str, tuple[str, int, int]] = {
    "cover": (r"red herring prospectus", 4, 0),  # first pages: price band, issue size, dates
    "basis_for_issue_price": (r"basis for (the )?(issue|offer) price", 6, 15),  # peers + valuation ratios
    "objects_of_the_issue": (r"objects of the (issue|offer)", 4, 15),
    "financial_summary": (r"summary of (the |our )?(restated )?(consolidated )?(financial|selected)", 4, 8),
    "capital_structure": (r"capital structure", 3, 15),
}
MAX_TOTAL_PAGES = 28
_WS = re.compile(r"\s+")


def _norm(t: str) -> str:
    """PDF text extraction scatters spaces and newlines through headings; collapse them."""
    return _WS.sub(" ", t).lower()


def find_pages(page_texts: list[str]) -> dict[str, list[int]]:
    """Return 1-based page numbers per section.

    Skips table-of-contents pages (many headings on one page) and the front matter before each section's
    earliest plausible start; prefers pages where the heading is the page title (first ~300 chars).
    """
    texts = [_norm(t) for t in page_texts]
    heads = {k: re.compile(v[0], re.IGNORECASE) for k, v in SECTIONS.items() if k != "cover"}
    toc_like = {i for i, t in enumerate(texts) if sum(1 for rx in heads.values() if rx.search(t)) >= 3}
    out: dict[str, list[int]] = {"cover": [p for p in (1, 2, 3, 4) if p <= len(texts)]}
    for name, rx in heads.items():
        _, limit, min_idx = SECTIONS[name]
        ok = [i for i in range(len(texts)) if i >= min_idx and i not in toc_like]
        hits = [i for i in ok if rx.search(texts[i][:300])] or [i for i in ok if rx.search(texts[i])]
        pages: list[int] = []
        for h in hits[:1]:  # first real occurrence is the section start
            pages += [h + k + 1 for k in range(limit) if h + k < len(texts)]
        out[name] = pages
    return out


def page_texts(pdf_path: Path, only: list[int] | None = None) -> list[str]:
    """Text of every page (or of the 0-based pages in `only`). pdfium is ~50x faster than pypdf on big RHPs."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        out = []
        for i in (range(len(pdf)) if only is None else only):
            page = pdf[i]
            tp = page.get_textpage()
            out.append(tp.get_text_range())
            tp.close()
            page.close()
        return out
    finally:
        pdf.close()


def build_excerpt(pdf_path: Path) -> tuple[bytes, list[int], dict[str, list[int]]]:
    """Return (excerpt_pdf_bytes, original_page_numbers_in_order, sections)."""
    reader = PdfReader(str(pdf_path))
    texts = page_texts(pdf_path)
    sections = find_pages(texts)
    wanted: list[int] = []
    for pages in sections.values():
        for p in pages:
            if p not in wanted:
                wanted.append(p)
    wanted = sorted(wanted)[:MAX_TOTAL_PAGES]
    w = PdfWriter()
    for p in wanted:
        w.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue(), wanted, sections


SCHEMA_HINT = """Return JSON with exactly these keys (use null when not found; never guess):
{
 "company": str, "document_type": "RHP"|"DRHP"|"other",
 "issue": {"price_band_low": num, "price_band_high": num, "face_value": num, "lot_size": int,
           "fresh_issue_cr": num, "offer_for_sale_cr": num, "total_issue_cr": num,
           "open_date": str, "close_date": str, "exchanges": [str], "page": int},
 "financials": [ {"fiscal_year": str, "revenue_cr": num, "ebitda_cr": num, "pat_cr": num, "net_worth_cr": num,
                  "total_borrowings_cr": num, "page": int} ],
 "ratios": {"eps": num, "pe_at_upper_band": num, "roe_pct": num, "ronw_pct": num, "nav_per_share": num,
            "as_of": str, "page": int},
 "objects_of_issue": [ {"purpose": str, "amount_cr": num, "page": int} ],
 "promoter_holding_pct": {"pre_issue": num, "post_issue": num, "page": int},
 "peers": [ {"name": str, "revenue_cr": num, "pe": num, "roe_pct": num, "eps": num, "page": int} ],
 "notes": [str]
}
Amounts in INR crore (convert from lakh/million if needed and say so in notes). Every "page" must be the
ORIGINAL RHP page number using the mapping given, not the position inside the excerpt."""


def extract_facts(client: Any, model: str, pdf_bytes: bytes, original_pages: list[int]) -> dict[str, Any]:
    from google.genai import types

    mapping = ", ".join(f"excerpt p{i + 1}=RHP p{p}" for i, p in enumerate(original_pages))
    prompt = (
        "You are extracting facts from excerpts of an Indian IPO Red Herring Prospectus. Use ONLY what the "
        "excerpt shows. " + SCHEMA_HINT + f"\n\nPAGE MAPPING: {mapping}"
    )
    from ..gemini.retry import with_retry

    resp = with_retry(lambda: client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
    ))
    return json.loads(resp.text)


def latest_revenue(data: dict[str, Any]) -> float | None:
    """Revenue (INR crore) of the most recent fiscal year that has one."""
    rows = [f for f in data.get("financials") or [] if isinstance(f.get("revenue_cr"), (int, float))]
    if not rows:
        return None
    return max(rows, key=lambda f: str(f.get("fiscal_year", "")))["revenue_cr"]


def closest_peer(data: dict[str, Any]) -> dict[str, Any] | None:
    """The single RHP peer closest to the IPO company in revenue size (ratio distance, so 2x and 0.5x tie)."""
    import math

    mine = latest_revenue(data)
    peers = [p for p in data.get("peers") or [] if isinstance(p.get("revenue_cr"), (int, float)) and p["revenue_cr"] > 0]
    if not peers:
        return None
    if not mine or mine <= 0:
        return peers[0]
    return min(peers, key=lambda p: abs(math.log(p["revenue_cr"] / mine)))


def format_facts(data: dict[str, Any]) -> str:
    """Plain-text FACTS block (with page refs) that post drafts are allowed to cite."""
    lines = [f"Company: {data.get('company')} ({data.get('document_type')})"]
    biz = data.get("business") or {}
    if biz.get("text"):
        lines.append(f"Business (RHP p.{biz.get('page')}, the company's own words): {biz['text']}")
    iss = data.get("issue") or {}
    if iss:
        lines.append(
            f"Issue: fresh issue {iss.get('fresh_issue_cr')} cr, opens {iss.get('open_date')}, "
            f"closes {iss.get('close_date')} [RHP p.{iss.get('page')}]"
        )
        from .enrich import issue_lines  # local import: enrich imports this module

        lines += issue_lines(data)
    for f in data.get("financials") or []:
        lines.append(
            f"{f.get('fiscal_year')}: revenue {f.get('revenue_cr')} cr, EBITDA {f.get('ebitda_cr')} cr, PAT {f.get('pat_cr')} cr "
            f"[RHP p.{f.get('page')}]"
        )
    r = data.get("ratios") or {}
    if r:
        lines.append(f"Ratios ({r.get('as_of')}): EPS {r.get('eps')}, P/E at upper band {r.get('pe_at_upper_band')}, "
                     f"ROE {r.get('roe_pct')}%, RoNW {r.get('ronw_pct')}% [RHP p.{r.get('page')}]")
    for o in data.get("objects_of_issue") or []:
        lines.append(f"Object: {o.get('purpose')} - {o.get('amount_cr')} cr [RHP p.{o.get('page')}]")
    ph = data.get("promoter_holding_pct") or {}
    if ph:
        lines.append(f"Promoter holding: {ph.get('pre_issue')}% pre, {ph.get('post_issue')}% post [RHP p.{ph.get('page')}]")
    for p in data.get("peers") or []:
        lines.append(f"Peer {p.get('name')}: revenue {p.get('revenue_cr')} cr, P/E {p.get('pe')}, ROE {p.get('roe_pct')}% [RHP p.{p.get('page')}]")
    return "\n".join(lines)


# ---------------------------------------------------------------- automatic verification
_NUM_RX = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?")
UNIT_FACTORS = (1, 10, 100)  # RHP tables are usually in INR million (x10 -> crore factor) or lakh (x100)


_SPLIT_DIGITS = re.compile(r"(?<=[\d,.])\s+(?=[\d,.])")


def _page_numbers(text: str) -> set[float]:
    """Numbers on a page, read both as printed and with digit runs split across lines re-joined."""
    out: set[float] = set()
    for tok in _NUM_RX.findall(text) + _NUM_RX.findall(_SPLIT_DIGITS.sub("", text)):
        t = tok.strip("()").replace(",", "")
        try:
            out.add(abs(float(t)))
        except ValueError:
            pass
    return out


def _appears(value: float, nums: set[float]) -> bool:
    for f in UNIT_FACTORS:
        target = abs(value) * f
        if any(abs(n - target) <= max(0.006, abs(target) * 0.0006) for n in nums):
            return True
    return False


def _cited_values(data: dict[str, Any]) -> list[tuple[str, float, int]]:
    """(label, value, original_page) for every numeric field that carries a page."""
    out: list[tuple[str, float, int]] = []

    def add(prefix: str, d: dict[str, Any] | None) -> None:
        if not isinstance(d, dict) or not isinstance(d.get("page"), int):
            return
        for k, v in d.items():
            if k != "page" and isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append((f"{prefix}.{k}", float(v), d["page"]))

    add("issue", data.get("issue"))
    add("ratios", data.get("ratios"))
    add("promoter_holding_pct", data.get("promoter_holding_pct"))
    for i, f in enumerate(data.get("financials") or []):
        add(f"financials[{f.get('fiscal_year', i)}]", f)
    for i, o in enumerate(data.get("objects_of_issue") or []):
        add(f"objects[{i}]", o)
    for p in data.get("peers") or []:
        add(f"peer[{p.get('name')}]", p)
    return out


CRITICAL = ("revenue_cr", "pat_cr", "price_band_high", "price_band_low", "total_issue_cr", "pe_at_upper_band")


def check_arithmetic(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    iss = data.get("issue") or {}
    lo, hi = iss.get("price_band_low"), iss.get("price_band_high")
    if lo and hi and lo > hi:
        issues.append("price band low is above high")
    fresh, ofs, total = iss.get("fresh_issue_cr"), iss.get("offer_for_sale_cr"), iss.get("total_issue_cr")
    if None not in (fresh, ofs, total) and total and abs((fresh + ofs) - total) > max(1.0, 0.02 * total):
        issues.append(f"fresh ({fresh}) + OFS ({ofs}) != total issue ({total})")
    for f in data.get("financials") or []:
        rev, pat = f.get("revenue_cr"), f.get("pat_cr")
        if rev and pat is not None and pat > rev:
            issues.append(f"{f.get('fiscal_year')}: PAT exceeds revenue")
        if rev is not None and rev <= 0:
            issues.append(f"{f.get('fiscal_year')}: non-positive revenue")
    years = [f.get("fiscal_year") for f in data.get("financials") or []]
    if len(set(years)) != len(years):
        issues.append("duplicate fiscal years")
    if len(years) < 3:
        issues.append(f"only {len(years)} fiscal year(s) extracted (expected 3)")
    return issues


def _owner(data: dict[str, Any], label: str) -> dict[str, Any] | None:
    """The dict that owns the field named by `label` (e.g. 'financials[FY23].revenue_cr')."""
    head = label.rsplit(".", 1)[0]
    if head in ("issue", "ratios", "promoter_holding_pct"):
        return data.get(head)
    m = re.match(r"(financials|objects|peer)\[(.*)\]$", head)
    if not m:
        return None
    kind, key = m.groups()
    if kind == "financials":
        return next((f for f in data.get("financials") or [] if str(f.get("fiscal_year", "")) == key), None)
    if kind == "objects" and key.isdigit():
        return data["objects_of_issue"][int(key)]
    if kind == "peer":
        return next((p for p in data.get("peers") or [] if str(p.get("name")) == key), None)
    return None


def _set_page(data: dict[str, Any], label: str, new_page: int) -> None:
    owner = _owner(data, label)
    if owner is not None:
        owner["page"] = new_page


def _null_field(data: dict[str, Any], label: str) -> None:
    owner = _owner(data, label)
    if owner is not None:
        owner[label.rsplit(".", 1)[-1]] = None


# Label that must precede a financial figure on its page: guards against a right number from the wrong row.
_ROW_LABELS = {
    "revenue_cr": r"revenue from operations|total revenue|total income|revenue",
    "ebitda_cr": r"ebitda|earnings before interest",
    "pat_cr": r"profit(?: after tax)?(?: for the (?:year|period))?|\bpat\b|net profit",
    "net_worth_cr": r"net worth",
    "total_borrowings_cr": r"borrowings|total debt",
}


def _in_labelled_row(field: str, value: float, text: str) -> bool:
    """True if `value` appears within ~220 characters after a label for `field` (i.e. in that table row)."""
    t = " ".join(text.split())
    for m in re.finditer(_ROW_LABELS[field], t, re.IGNORECASE):
        if _appears(value, _page_numbers(t[m.end(): m.end() + 220])):
            return True
    return False


def verify_extraction(pdf_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Check each cited figure appears on its cited page (unit-tolerant), and in the right labelled row for
    financial figures, plus arithmetic sanity. This replaces a second paid extraction pass.

    If a figure is not on the cited page but IS on another page that was sent, the citation is corrected
    (and reported); a figure that fails a check is removed so it can never reach a post.
    """
    reader = PdfReader(str(pdf_path))
    sent = sorted({p for v in (data.get("_excerpt_pages") or {}).values() for p in v})
    texts: dict[int, str] = {}

    def text(page: int) -> str:
        if page not in texts:
            texts[page] = page_texts(pdf_path, [page - 1])[0]
        return texts[page]

    def nums(page: int) -> set[float]:
        return _page_numbers(text(page))

    checked, missing, corrected = 0, [], []
    for label, value, page in _cited_values(data):
        checked += 1
        field = label.rsplit(".", 1)[-1]
        if label.startswith("financials[") and field in _ROW_LABELS:
            # figure must sit in its labelled row (e.g. 'Profit for the year ...'); try the cited page, then the rest
            pages = [page] + sorted((q for q in sent if q != page), key=lambda q: abs(q - page))
            hit = next((q for q in pages if 1 <= q <= len(reader.pages) and _in_labelled_row(field, value, text(q))), None)
            if hit is None:
                missing.append({"field": label, "value": value, "page": page, "why": "not found in its labelled row on any sent page; removed"})
                _null_field(data, label)
            elif hit != page:
                corrected.append({"field": label, "value": value, "from_page": page, "to_page": hit})
                _set_page(data, label, hit)
            continue
        where = page if (1 <= page <= len(reader.pages) and _appears(value, nums(page))) else None
        if where is None:
            where = next((q for q in sorted(sent, key=lambda q: abs(q - page)) if q != page and _appears(value, nums(q))), None)
            if where is None:
                missing.append({"field": label, "value": value, "page": page, "why": "not found on any sent page; removed"})
                _null_field(data, label)  # never let an unverifiable number reach a post
                continue
            corrected.append({"field": label, "value": value, "from_page": page, "to_page": where})
            _set_page(data, label, where)
    arithmetic = check_arithmetic(data)
    critical_missing = [m for m in missing if m["field"].rsplit(".", 1)[-1] in CRITICAL]
    rate = 1 - len(missing) / checked if checked else 0.0
    passed = bool(checked) and not critical_missing and not arithmetic and rate >= 0.8
    return {"checked": checked, "match_rate": round(rate, 3), "page_corrections": corrected, "missing": missing,
            "arithmetic_issues": arithmetic, "critical_missing": critical_missing, "passed": passed}


def consensus_filter(a: dict[str, Any], b: dict[str, Any], rel_tol: float = 0.002) -> list[dict[str, Any]]:
    """Null every numeric figure in `a` that a second independent extraction `b` does not reproduce.

    LLM extractions of dense tables vary run to run; a figure two runs agree on is far likelier to be right.
    Labels come from `a`; a label absent from `b` counts as a disagreement. Returns the dropped figures.
    """
    other = {label: value for label, value, _ in _cited_values(b)}
    dropped: list[dict[str, Any]] = []
    for label, value, page in _cited_values(a):
        v2 = other.get(label)
        if v2 is None or abs(v2 - value) > rel_tol * max(abs(value), 1e-9) + 1e-9:
            dropped.append({"field": label, "run_a": value, "run_b": v2})
            _null_field(a, label)
    return dropped
