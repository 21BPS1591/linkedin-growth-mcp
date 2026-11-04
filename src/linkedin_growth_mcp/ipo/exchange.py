"""Listing-day prices straight from the exchanges' official end-of-day files (bhavcopy), the primary source.

NSE:  nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip
BSE:  www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_<YYYYMMDD>_F_0000.CSV
Files are cached under data/exchange and never deleted. Names are matched on text, and on NSE the previous close of a listing
day equals the issue price, which is used to tell look-alike names apart."""
from __future__ import annotations

import csv
import difflib
import io
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import httpx

URLS = {
    "NSE": "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip",
    "BSE": "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d}_F_0000.CSV",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Accept": "*/*"}


def _norm(name: str) -> str:
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"\b(limited|ltd|pvt|private)\b", " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def fetch_day(exchange: str, day: date, cache_dir: Path, timeout: float = 60.0) -> list[dict[str, str]] | None:
    """All rows of that exchange's file for the trading day, or None if it is not published (holiday, or too early)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{exchange}_{day.strftime('%Y%m%d')}.csv"
    if f.exists():
        data = f.read_bytes()
    else:
        r = httpx.get(URLS[exchange].format(d=day.strftime("%Y%m%d")), headers=HEADERS, timeout=timeout, follow_redirects=True)
        if r.status_code != 200 or len(r.content) < 500:
            return None
        data = r.content
        if data[:2] == b"PK":
            z = zipfile.ZipFile(io.BytesIO(data))
            data = z.read(z.namelist()[0])
        f.write_bytes(data)
    return list(csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace"))))


def _f(x: str | None) -> float | None:
    try:
        v = float(x) if x not in (None, "") else None
    except ValueError:
        return None
    return v


def find_listing(rows: list[dict[str, str]], company: str, issue_price: float | None = None) -> dict[str, Any] | None:
    """The equity row for `company`, as numbers. Best text match wins; on NSE the listing day's previous close must equal the
    issue price (when it is given) so a different company with a similar name cannot be picked."""
    want = _norm(company)
    best, best_score = None, 0.0
    for r in rows:
        if r.get("SctySrs") in ("", None) or r.get("FinInstrTp") == "OPT":
            continue
        name = _norm(r.get("FinInstrmNm", ""))
        if not name:
            continue
        score = 1.0 if name == want else (0.95 if (want in name or name in want) and min(len(want), len(name)) >= 6 else difflib.SequenceMatcher(None, want, name).ratio())
        prev = _f(r.get("PrvsClsgPric"))
        price_ok = bool(issue_price and prev and abs(prev - issue_price) / issue_price <= 0.005)
        if score < 0.88:
            # NSE abbreviates names ("SAHAJANAND MEDICAL TECH L"): accept the same first word when the previous close equals the issue price
            first = re.sub(r"[^a-z0-9]", "", company.lower().split()[0]) if company.split() else ""
            if not (price_ok and len(first) >= 4 and _norm(r.get("FinInstrmNm", "")).startswith(first)):
                continue
            score = 0.9
        if issue_price and prev and not price_ok:
            continue  # on NSE the previous close of a listing day is the issue price
        if score > best_score:
            best, best_score = r, score
    if best is None:
        return None
    return {"symbol": best.get("TckrSymb"), "name": best.get("FinInstrmNm"), "series": best.get("SctySrs"), "isin": best.get("ISIN"),
            "open": _f(best.get("OpnPric")), "high": _f(best.get("HghPric")), "low": _f(best.get("LwPric")),
            "close": _f(best.get("ClsPric")), "last": _f(best.get("LastPric")), "prev_close": _f(best.get("PrvsClsgPric")),
            "volume": _f(best.get("TtlTradgVol")), "match_score": round(best_score, 3)}
