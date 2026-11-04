"""Chittorgarh IPO pages -> flat facts. Plain HTTP + HTML tables, no LLM and no browser.

Every table on an IPO page becomes facts named "<section>|<row label>|<column header>", so new rows or sections are
captured without code changes. Third-party data: callers must tag it as a secondary source."""
from __future__ import annotations

import io
import re
from datetime import date
from typing import Any

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.chittorgarh.com"
DASHBOARD = BASE + "/ipo/ipo_dashboard.asp"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
_NUM = re.compile(r"^[^\d\-]*(-?\d[\d,]*\.?\d*)")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def get(url: str, timeout: float = 30.0) -> httpx.Response:
    r = httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r


def to_num(text: str) -> float | None:
    """First number of a cell like '2,02,28,204 shares (agg. up to ...)', '₹14,850', '21.25%', '0.04x'. Dates and prose give None."""
    t = text.strip()
    if not t or re.match(r"^[A-Za-z]{3},? ", t):  # 'Tue, Nov 3, 2026'
        return None
    m = _NUM.match(t)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_dashboard(html: str) -> list[dict[str, Any]]:
    """[{cg_id, slug, name, url, dates}] from the 'Mainboard IPOs' table."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = re.match(r"^/ipo/([a-z0-9\-]+)/(\d+)/$", a["href"])
        if not m or m.group(2) in seen:
            continue
        row = a.find_parent("tr")
        if row is None:  # only table rows are the IPO list; other links are menus
            continue
        seen.add(m.group(2))
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        text = cells[0] if cells else a.get_text(" ", strip=True)
        dm = re.search(r"(\d{1,2}(?: [A-Za-z]{3})? (?:-|to) \d{1,2} [A-Za-z]{3})\s*$", text)
        out.append({"cg_id": int(m.group(2)), "slug": m.group(1), "url": BASE + a["href"],
                    "name": re.sub(r" [A-Z]$", "", (text[: dm.start()] if dm else text).strip()), "dates": dm.group(1) if dm else ""})
    return out


def issue_window(dates: str, today: date | None = None) -> tuple[date, date] | None:
    """'27 - 29 Oct' or '30 Oct - 03 Nov' -> (open, close), assuming the year closest to today."""
    today = today or date.today()
    m = re.match(r"^(\d{1,2})(?: ([A-Za-z]{3}))? (?:-|to) (\d{1,2}) ([A-Za-z]{3})$", dates.strip())
    if not m:
        return None
    d1, m1, d2, m2 = int(m.group(1)), (m.group(2) or m.group(4)).lower(), int(m.group(3)), m.group(4).lower()
    if m1 not in _MONTHS or m2 not in _MONTHS:
        return None
    try:
        start, end = date(today.year, _MONTHS[m1], d1), date(today.year, _MONTHS[m2], d2)
    except ValueError:
        return None
    if end < start:
        end = end.replace(year=end.year + 1)
    return start, end


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_tables(html: str) -> list[dict[str, Any]]:
    """Every table cell as {field, text, num}. Two-column tables are label/value; wider ones row-label x column-header."""
    soup = BeautifulSoup(html, "lxml")
    facts: list[dict[str, Any]] = []
    section = ""
    for t in soup.find_all("table"):
        h = t.find_previous(["h2", "h3"])
        sec = _clean(h.get_text(" ", strip=True)) if h else ""
        section = sec or section
        rows = [[_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])] for tr in t.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        if all(len(r) == 2 for r in rows):  # label | value
            for label, val in rows:
                if label and val:
                    facts.append({"field": f"{section}|{label}", "text": val, "num": to_num(val)})
            continue
        header, body = rows[0], rows[1:]
        for r in body:
            if not r or not r[0]:
                continue
            for j, val in enumerate(r[1:], 1):
                col = header[j] if j < len(header) and header[j] else f"col{j}"
                if val:
                    facts.append({"field": f"{section}|{r[0]}|{col}", "text": val, "num": to_num(val)})
    return facts


def selling_shareholders(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows of the 'Offer For Sale - Selling Shareholders' table (name -> category, shares, amount)."""
    rows: dict[str, dict[str, Any]] = {}
    for f in facts:
        parts = f["field"].split("|")
        if len(parts) == 3 and parts[0].startswith("Offer For Sale"):
            d = rows.setdefault(parts[1], {"name": parts[1]})
            if parts[2].startswith("Category"):
                d["category"] = f["text"]
            elif parts[2].startswith("No. of Shares"):
                d["shares"] = int(f["num"]) if f["num"] is not None else None
            elif parts[2].startswith("Amount"):
                d["amount_cr"] = f["num"]
    return [d for n, d in rows.items() if d.get("shares") and not n.lower().startswith("total")]


def anchor_letter_url(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(".pdf") and "anchor" in a["href"].lower():
            return a["href"]
    return None


_HEADER_END = re.compile(r"Total amount allocated at the Anchor Investor Allocation Price \(₹\)")
_ROW = re.compile(r"(?:^| )(\d{1,2})\. ([A-Z][^%]*?) ([\d,]+) ([\d.]+)% ([\d.]+) ([\d,]+\.\d\d)")
_TOTAL = re.compile(r"Total ([\d,]+) 100\.00% - ([\d,]+\.\d\d)")


_OCR_ROW_START = re.compile(r"^(\d{1,2})\s*[.,]\s*(.*)$")
_OCR_ROW_TAIL = re.compile(r"^(.*?)\s+([\d,]{4,})\s+([\d.]+)\s*%?\s+(\d+(?:\.\d+)?)\s+([\d,]{4,}(?:\.\d\d)?)$")
_OCR_TOTAL = re.compile(r"^(?:Total\s+)?([\d,]{4,})\s+100(?:\.0+)?\s*%?\s+(?:-\s+)?([\d,]{4,}(?:\.\d\d)?)$", re.IGNORECASE)


def _ocr_lines(pdf_bytes: bytes) -> list[str]:
    """Render each PDF page and OCR it locally (RapidOCR), regrouping text boxes into reading-order lines."""
    import numpy as np
    import pypdfium2 as pdfium
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    lines: list[str] = []
    pdf = pdfium.PdfDocument(pdf_bytes)
    for i in range(len(pdf)):
        res, _ = ocr(np.array(pdf[i].render(scale=3).to_pil().convert("RGB")))
        items = sorted((r[0][0][1], r[0][0][0], r[1]) for r in res or [])
        cur: list[tuple[float, str]] = []
        y: float | None = None
        for yy, x, t in items:
            if y is not None and abs(yy - y) >= 18:
                lines.append(" ".join(s for _, s in sorted(cur)))
                cur = []
                y = None
            if y is None:
                y = yy
            cur.append((x, t))
        if cur:
            lines.append(" ".join(s for _, s in sorted(cur)))
    return lines


def _parse_scanned_anchor_letter(pdf_bytes: bytes) -> dict[str, Any]:
    """OCR path for image-only letters. Same safety rule as the text path: rows are returned only if they add up to the
    letter's own total. OCR drops spaces, so names are re-spaced at lowercase->uppercase joins (best effort)."""
    try:
        lines = _ocr_lines(pdf_bytes)
    except Exception as e:
        return {"ok": False, "reason": f"scanned letter and OCR failed: {str(e)[:80]}", "rows": []}
    rows: list[dict[str, Any]] = []
    started = 0          # serial numbers seen so far; the next row must be started + 1
    buf = ""             # text of the row being assembled (names may wrap over lines)
    row: dict[str, Any] | None = None   # set once the row's numbers were read; later lines extend its name
    total = None
    for ln in (s.strip() for s in lines):
        if not ln:
            continue
        t = _OCR_TOTAL.match(ln)
        if t and rows:
            total = (int(t.group(1).replace(",", "")), float(t.group(2).replace(",", "")))
            break
        m = _OCR_ROW_START.match(ln)
        if m and int(m.group(1)) == started + 1:
            started += 1
            buf, row = m.group(2), None
        elif started and row is None:
            buf += " " + ln
        elif row is not None:
            row["name"] += " " + ln
            continue
        else:
            continue
        tail = _OCR_ROW_TAIL.match(buf)
        if tail and row is None:
            row = {"name": tail.group(1).strip(), "shares": int(tail.group(2).replace(",", "")), "pct": float(tail.group(3)),
                   "price": float(tail.group(4)), "amount": float(tail.group(5).replace(",", ""))}
            rows.append(row)
    if total is None:
        return {"ok": False, "reason": "scanned letter: no total line found by OCR", "rows": []}
    for r in rows:
        r["name"] = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"\s+", " ", r["name"])).strip()
        n = r["name"].lower()
        r["category"] = "Insurance" if "insurance" in n else "Mutual Fund" if "mutual fund" in n else "Other"
    want_shares, want_amt = total
    ok = bool(rows) and sum(r["shares"] for r in rows) == want_shares and abs(sum(r["amount"] for r in rows) - want_amt) < 1
    return {"ok": ok, "total_shares": want_shares, "total_amount": want_amt, "rows": rows,
            "reason": "" if ok else f"OCR rows add up to {sum(r['shares'] for r in rows)} shares, letter says {want_shares}"}


def parse_anchor_letter(pdf_bytes: bytes) -> dict[str, Any]:
    """Company letter to the exchanges listing every anchor allocation. Rows are read below each page's table header
    (so a repeated page header never leaks into a name) and are only returned if they add up to the letter's own total."""
    from pypdf import PdfReader

    pages = []
    for p in PdfReader(io.BytesIO(pdf_bytes)).pages:
        try:
            pages.append(_clean(p.extract_text() or ""))
        except Exception:
            pages.append("")
    text = " ".join(pages)
    if len(text.strip()) < 100:  # scanned letter: no text layer, so read it with OCR
        return _parse_scanned_anchor_letter(pdf_bytes)
    total = _TOTAL.search(text)
    if not total:
        return {"ok": False, "reason": "no total line found", "rows": []}
    want_shares, want_amt = int(total.group(1).replace(",", "")), float(total.group(2).replace(",", ""))
    rows: list[dict[str, Any]] = []
    for pg in pages:
        m = _HEADER_END.search(pg)
        if not m:
            continue
        body = pg[m.end():]
        cut = _TOTAL.search(body)
        body = body[: cut.start()] if cut else body
        for r in _ROW.finditer(body):
            rows.append({"name": r.group(2).strip(), "shares": int(r.group(3).replace(",", "")), "pct": float(r.group(4)),
                         "price": float(r.group(5)), "amount": float(r.group(6).replace(",", ""))})
        if cut:
            break  # the main table ends at its total; later tables repeat mutual funds
    ok = bool(rows) and sum(r["shares"] for r in rows) == want_shares and abs(sum(r["amount"] for r in rows) - want_amt) < 1
    mf = ""
    i = text.find("scheme-wise details")
    if i >= 0:
        mf = text[i:]
    for r in rows:
        r["category"] = ("Insurance" if "INSURANCE" in r["name"] else "Mutual Fund" if r["name"] in mf else "FPI")
    return {"ok": ok, "total_shares": want_shares, "total_amount": want_amt, "rows": rows,
            "reason": "" if ok else f"rows add up to {sum(r['shares'] for r in rows)} shares, letter says {want_shares}"}
