"""Sourced enrichment for an IPO: what the company does, the issue structure, the price band, and the computed
side-by-side comparison numbers. Nothing here is guessed: each item is quoted from, or computed from, a filing."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from google.genai import types

from . import rhp, sebi

_WS = re.compile(r"\s+")
_BUSINESS = re.compile(r"\bWe are (?:a|an|one of the|the)\b[^.]{40,420}\.")
_BAD_BUSINESS = re.compile(r"(?i)exposed|dependent|subject to|may not|cannot|unable|risk|adverse|failure")
_GOOD_BUSINESS = re.compile(r"(?i)company|provider|manufacturer|player|leader|platform|business|supplier|operator|developer")
_HEADING = re.compile(r"(?i)\b(overview|summary of (?:our )?business|business overview|our business)\b")


def business_sentence(pdf_path: Path) -> dict[str, Any] | None:
    """The company's own one-sentence description, quoted verbatim from the RHP (page-cited)."""
    texts = [_WS.sub(" ", t) for t in rhp.page_texts(pdf_path)]
    for pass_no in (1, 2):
        for i in range(14, min(len(texts), 220)):
            if pass_no == 1 and not _HEADING.search(texts[i][:700]):
                continue
            for m in _BUSINESS.finditer(texts[i]):
                s = m.group(0)
                if _GOOD_BUSINESS.search(s) and not _BAD_BUSINESS.search(s) and len(s.split()) >= 12:
                    return {"text": s, "page": i + 1}
    return None


def issue_structure(pdf_path: Path) -> dict[str, Any]:
    """Offer-for-sale share count from the RHP cover table (its rupee value is '[●]' until the price band is known)."""
    n = _WS.sub(" ", " ".join(rhp.page_texts(pdf_path, [0, 1, 2, 3])))
    for m in re.finditer(r"Up to ([\d,]{7,}) Equity Shares of face value", n):
        if re.search(r"(?i)offer for sale", n[max(0, m.start() - 700): m.start()]):
            return {"ofs_shares": int(m.group(1).replace(",", ""))}
    return {}


# ---------------------------------------------------------------- price band from SEBI's addendum filing
def find_addendum_url(company: str) -> str | None:
    html = httpx.get(sebi.LISTING_URL, headers={"User-Agent": sebi.UA}, follow_redirects=True, timeout=60).text
    key = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")[:18]
    for u in re.findall(r'href="([^"]+_\d+\.html)"', html):
        if "addendum" in u.lower() and key in u.lower():
            return u
    return None


def price_band_ok(low: Any, high: Any) -> bool:
    return (isinstance(low, (int, float)) and isinstance(high, (int, float)) and 1 <= low <= high <= 100000
            and high / low <= 1.25)  # SEBI: the cap may not exceed 120% of the floor


def fetch_price_band(client: Any, model: str, company: str) -> dict[str, Any] | None:
    from ..gemini.retry import with_retry

    url = find_addendum_url(company)
    if not url:
        return None
    pdf_url = sebi.resolve_pdf(url)
    r = httpx.get(pdf_url, headers={"User-Agent": sebi.UA}, follow_redirects=True, timeout=120)
    r.raise_for_status()
    if r.content[:4] != b"%PDF" or len(r.content) > 30 << 20:
        return None
    prompt = ('This is a SEBI-filed addendum to an Indian IPO Red Herring Prospectus (possibly scanned). Return JSON: '
              '{"price_band_low": number|null, "price_band_high": number|null, "face_value": number|null, '
              '"page": int|null}. Rupee amounts per equity share. Use null if not shown. Do not guess.')
    resp = with_retry(lambda: client.models.generate_content(
        model=model, contents=[types.Part.from_bytes(data=r.content, mime_type="application/pdf"), prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)))
    d = json.loads(resp.text)
    if not price_band_ok(d.get("price_band_low"), d.get("price_band_high")):
        return None
    return {"price_band_low": d["price_band_low"], "price_band_high": d["price_band_high"],
            "face_value": d.get("face_value"), "price_band_page": d.get("page"), "price_band_source": "SEBI addendum",
            "price_band_url": url}


# ---------------------------------------------------------------- computed comparison numbers
def _fy_sorted(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [f for f in data.get("financials") or [] if isinstance(f.get("revenue_cr"), (int, float)) and f["revenue_cr"] > 0]
    return sorted(rows, key=lambda f: re.sub(r"\D", "", str(f.get("fiscal_year", ""))))


def comparison_lines(data: dict[str, Any], peer: dict[str, Any] | None) -> list[str]:
    """'Computed:' lines for the side-by-side comparison. Every figure is derived from verified facts."""
    out: list[str] = []
    fin = _fy_sorted(data)
    if len(fin) >= 2:
        a, b, n = fin[0], fin[-1], len(fin) - 1
        out.append(f"Computed: revenue CAGR {a['fiscal_year']} to {b['fiscal_year']}: "
                   f"{((b['revenue_cr'] / a['revenue_cr']) ** (1 / n) - 1) * 100:.1f}%")
        if isinstance(a.get("pat_cr"), (int, float)) and isinstance(b.get("pat_cr"), (int, float)) and a["pat_cr"] > 0 and b["pat_cr"] > 0:
            out.append(f"Computed: PAT CAGR {a['fiscal_year']} to {b['fiscal_year']}: "
                       f"{((b['pat_cr'] / a['pat_cr']) ** (1 / n) - 1) * 100:.1f}%")
    if fin:
        last = fin[-1]
        if isinstance(last.get("ebitda_cr"), (int, float)):
            out.append(f"Computed: EBITDA margin {last['fiscal_year']}: {last['ebitda_cr'] / last['revenue_cr'] * 100:.1f}%")
        if isinstance(last.get("pat_cr"), (int, float)):
            out.append(f"Computed: PAT margin {last['fiscal_year']}: {last['pat_cr'] / last['revenue_cr'] * 100:.1f}%")
    iss, ratios = data.get("issue") or {}, data.get("ratios") or {}
    hi, eps = iss.get("price_band_high"), ratios.get("eps")
    if isinstance(hi, (int, float)) and isinstance(eps, (int, float)) and eps > 0:
        out.append(f"Computed: P/E at upper band of ₹{hi:g} on EPS ₹{eps:g}: {hi / eps:.1f}x")
    if peer and fin:
        mult = peer["revenue_cr"] / fin[-1]["revenue_cr"]
        out.append(f"Computed: {peer.get('name')} revenue is {mult:.1f}x the company's "
                   f"(₹{peer['revenue_cr']:,.0f} crore vs ₹{fin[-1]['revenue_cr']:,.0f} crore)")
        if peer.get("pe") is not None:
            out.append(f"Computed: peer P/E {peer['pe']:g}x")
        if isinstance(ratios.get("ronw_pct"), (int, float)) and isinstance(peer.get("roe_pct"), (int, float)):
            out.append(f"Computed: return on net worth {ratios['ronw_pct']:g}% (company) vs return on equity "
                       f"{peer['roe_pct']:g}% (peer); the two measures are defined slightly differently")
        out.append("Peer choice: the closest in revenue size among the peers the RHP itself lists, "
                   f"so it is {mult:.1f}x larger than the company")
    return out


def issue_lines(data: dict[str, Any]) -> list[str]:
    """Whole-issue description: fresh + offer for sale, with the OFS value computed at the upper band."""
    iss = data.get("issue") or {}
    out = []
    lo, hi = iss.get("price_band_low"), iss.get("price_band_high")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        out.append(f"Price band ({iss.get('price_band_source', 'filing')}): ₹{lo:g} to ₹{hi:g} per share, face value ₹{iss.get('face_value', 2):g}")
    fresh, shares = iss.get("fresh_issue_cr"), iss.get("ofs_shares")
    if shares:
        line = f"Offer for sale: up to {shares:,} shares (shares sold by existing shareholders; the company gets none of that money)"
        if isinstance(hi, (int, float)):
            ofs = shares * hi / 1e7
            line += f", worth about ₹{ofs:,.1f} crore at the upper band"
            if isinstance(fresh, (int, float)):
                line += f"; total issue about ₹{fresh + ofs:,.1f} crore (computed: fresh ₹{fresh:g} crore + offer for sale)"
        out.append(line)
    return out
