"""Flatten each IPO's stored RHP facts into dashboard/ML-ready rows. Nothing is guessed: missing inputs stay blank
and are listed in `missing_fields`; derived values are computed only from figures that are present."""
from __future__ import annotations

import re
from statistics import median
from typing import Any


def _fy_key(label: str) -> int:
    m = re.findall(r"\d+", str(label))
    if not m:
        return 0
    n = int(m[-1])
    return n + 2000 if n < 100 else n


def _div(a: float | None, b: float | None, pct: bool = False) -> float | None:
    if a is None or b in (None, 0):
        return None
    return round(a / b * (100 if pct else 1), 4)


def _cagr(first: float | None, last: float | None, years: int) -> float | None:
    if first is None or last is None or first <= 0 or last <= 0 or years < 1:
        return None
    return round(((last / first) ** (1 / years) - 1) * 100, 4)


def financial_rows(ipo_id: int, company: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per fiscal year with margins and year-on-year growth."""
    fins = sorted(data.get("financials") or [], key=lambda f: _fy_key(f.get("fiscal_year", "")))
    rows, prev = [], None
    for f in fins:
        rev, pat, ebitda = f.get("revenue_cr"), f.get("pat_cr"), f.get("ebitda_cr")
        rows.append({
            "ipo_id": ipo_id, "company": company, "fiscal_year": f.get("fiscal_year"),
            "revenue_cr": rev, "ebitda_cr": ebitda, "pat_cr": pat,
            "net_worth_cr": f.get("net_worth_cr"), "total_borrowings_cr": f.get("total_borrowings_cr"),
            "ebitda_margin_pct": _div(ebitda, rev, True), "pat_margin_pct": _div(pat, rev, True),
            "debt_to_net_worth": _div(f.get("total_borrowings_cr"), f.get("net_worth_cr")),
            "revenue_growth_pct": None if prev is None or not prev.get("revenue_cr") or rev is None
            else round((rev / prev["revenue_cr"] - 1) * 100, 4),
            "pat_growth_pct": None if prev is None or not prev.get("pat_cr") or pat is None or prev["pat_cr"] <= 0
            else round((pat / prev["pat_cr"] - 1) * 100, 4),
            "source_page": f.get("page"),
        })
        prev = f
    return rows


def ipo_row(ipo_id: int, data: dict[str, Any], verified: bool, created_at: float) -> dict[str, Any]:
    issue, ratios = data.get("issue") or {}, data.get("ratios") or {}
    fins = sorted(data.get("financials") or [], key=lambda f: _fy_key(f.get("fiscal_year", "")))
    first, last = (fins[0], fins[-1]) if fins else ({}, {})
    years = _fy_key(last.get("fiscal_year", "")) - _fy_key(first.get("fiscal_year", "")) if fins else 0
    hi, lo, eps = issue.get("price_band_high"), issue.get("price_band_low"), ratios.get("eps")
    ofs_shares = issue.get("ofs_shares")
    ofs_cr = issue.get("offer_for_sale_cr")
    if ofs_cr is None and ofs_shares and hi:
        ofs_cr = round(ofs_shares * hi / 1e7, 3)  # shares x upper band, in crore
    fresh = issue.get("fresh_issue_cr")
    total = issue.get("total_issue_cr")
    if total is None and fresh is not None and ofs_cr is not None:
        total = round(fresh + ofs_cr, 3)
    peers = data.get("peers") or []
    pe = [p["pe"] for p in peers if p.get("pe") is not None]
    roe = [p["roe_pct"] for p in peers if p.get("roe_pct") is not None]
    closest = None
    if peers and last.get("revenue_cr"):
        with_rev = [p for p in peers if p.get("revenue_cr")]
        if with_rev:
            closest = min(with_rev, key=lambda p: abs(p["revenue_cr"] - last["revenue_cr"]))
    ver = data.get("_verification") or {}
    row = {
        "ipo_id": ipo_id, "company": data.get("company"), "verified": int(bool(verified)),
        "extraction_match_rate": ver.get("match_rate"), "extracted_at": created_at,
        "open_date": issue.get("open_date"), "close_date": issue.get("close_date"),
        "price_band_low": lo, "price_band_high": hi, "face_value": issue.get("face_value"), "lot_size": issue.get("lot_size"),
        "fresh_issue_cr": fresh, "offer_for_sale_cr": ofs_cr, "ofs_shares": ofs_shares, "total_issue_cr": total,
        "ofs_share_of_issue_pct": _div(ofs_cr, total, True),
        "exchanges": ",".join(issue.get("exchanges") or []),
        "latest_fy": last.get("fiscal_year"), "latest_revenue_cr": last.get("revenue_cr"),
        "latest_ebitda_cr": last.get("ebitda_cr"), "latest_pat_cr": last.get("pat_cr"),
        "latest_net_worth_cr": last.get("net_worth_cr"), "latest_borrowings_cr": last.get("total_borrowings_cr"),
        "latest_ebitda_margin_pct": _div(last.get("ebitda_cr"), last.get("revenue_cr"), True),
        "latest_pat_margin_pct": _div(last.get("pat_cr"), last.get("revenue_cr"), True),
        "revenue_cagr_pct": _cagr(first.get("revenue_cr"), last.get("revenue_cr"), years),
        "pat_cagr_pct": _cagr(first.get("pat_cr"), last.get("pat_cr"), years),
        "eps": eps, "pe_at_upper_band": _div(hi, eps) if hi and eps and eps > 0 else None,
        "pe_at_lower_band": _div(lo, eps) if lo and eps and eps > 0 else None,
        "roe_pct": ratios.get("roe_pct"), "ronw_pct": ratios.get("ronw_pct"), "nav_per_share": ratios.get("nav_per_share"),
        "price_to_nav": _div(hi, ratios.get("nav_per_share")),
        "promoter_holding_pre_pct": (data.get("promoter_holding_pct") or {}).get("pre_issue"),
        "promoter_holding_post_pct": (data.get("promoter_holding_pct") or {}).get("post_issue"),
        "objects_of_issue_count": len(data.get("objects_of_issue") or []),
        "peer_count": len(peers), "peer_median_pe": round(median(pe), 4) if pe else None,
        "peer_median_roe_pct": round(median(roe), 4) if roe else None,
        "closest_peer": closest.get("name") if closest else None,
        "closest_peer_revenue_multiple": _div(closest.get("revenue_cr"), last.get("revenue_cr")) if closest else None,
        "closest_peer_pe": closest.get("pe") if closest else None,
        "business": (data.get("business") or {}).get("text"),
    }
    row["missing_fields"] = ";".join(k for k, v in row.items() if v is None and k not in ("closest_peer",))
    return row


def apply_extras(row: dict[str, Any], extra: dict[str, Any], investors: list[Any]) -> None:
    """Overlay supplementary RHP facts (promoter holding, lot size) and pre-IPO investor totals, then redo missing_fields."""
    # Any numeric supplementary fact fills a blank column, or becomes a new one. Filing-based values are never overwritten.
    for field, r in extra.items():
        if r["value_num"] is None:
            continue
        if field not in row or row.get(field) is None:
            row[field] = r["value_num"]
    row["extra_sources"] = ";".join(sorted({r["source"].split(" (")[0] for r in extra.values()}))
    pre = [i for i in investors if i["round_type"] in ("pre_ipo", "private_placement", "angel")]
    row["pre_ipo_investor_count"] = len(pre)
    row["pre_ipo_shares"] = sum(i["shares"] or 0 for i in pre) or None
    prices = [i["price_per_share"] for i in pre if i["price_per_share"]]
    row["pre_ipo_price"] = prices[0] if prices and len(set(prices)) == 1 else None
    row["pre_ipo_raised_cr"] = round(sum((i["shares"] or 0) * (i["price_per_share"] or 0) for i in pre) / 1e7, 3) if pre else None
    hi = row.get("price_band_high")
    row["pre_ipo_price_vs_upper_band_pct"] = round((hi / row["pre_ipo_price"] - 1) * 100, 2) if hi and row["pre_ipo_price"] else None
    skip = ("closest_peer", "pre_ipo_shares", "pre_ipo_price", "pre_ipo_raised_cr", "pre_ipo_price_vs_upper_band_pct")
    row["missing_fields"] = ";".join(k for k, v in row.items() if v is None and k not in skip and k != "missing_fields")


POST_COLS = ["post_id", "post_published_at", "post_style", "post_hook", "post_impressions", "post_reactions",
             "post_comments", "post_reposts"]
