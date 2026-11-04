"""Find upcoming mainboard IPOs and download+validate their RHP PDFs.

Discovery uses Gemini with Google Search grounding; the model's answers are only leads. Anything downloaded
is validated against the actual PDF (header + 'RED HERRING PROSPECTUS' + company name) before it is trusted.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from dateutil import parser as dtparser
from google.genai import types
from pypdf import PdfReader

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
MAX_BYTES = 250 << 20
ALLOWED_HOSTS = ("bseindia.com", "nseindia.com", "sebi.gov.in")  # regulator-hosted copies only


def parse_date(s: Any) -> date | None:
    try:
        return dtparser.parse(str(s), fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return None


def _json_block(text: str) -> Any:
    m = re.search(r"\[.*\]|\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0)) if m else []


def discover_upcoming(client: Any, model: str, today: date, window_days: int = 10) -> list[dict[str, Any]]:
    prompt = (
        f"Today is {today.isoformat()}. Using web search, list MAINBOARD (not SME) IPOs in India that open for "
        f"subscription between {today.isoformat()} and {window_days} days from today. For each give: company, "
        "open_date (YYYY-MM-DD), close_date (YYYY-MM-DD), and rhp_url = a DIRECT link to the Red Herring "
        "Prospectus PDF hosted on bseindia.com, nseindia.com or sebi.gov.in if you can find one, else null. "
        "Return ONLY a JSON array of objects with keys company, open_date, close_date, rhp_url. "
        "Return [] if you find none. Do not guess."
    )
    resp = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.0),
    )
    items = _json_block(resp.text or "[]")
    out = []
    for it in items if isinstance(items, list) else []:
        o, c = parse_date(it.get("open_date")), parse_date(it.get("close_date"))
        if it.get("company") and o and c and today <= o <= date.fromordinal(today.toordinal() + window_days):
            out.append({"company": str(it["company"]).strip(), "open": o, "close": c, "rhp_url": it.get("rhp_url")})
    return out


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def download_rhp(url: str, company: str, dest_dir: Path) -> Path:
    """Download an RHP and prove it is one. Raises RuntimeError with the reason otherwise."""
    host = httpx.URL(url).host or ""
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise RuntimeError(f"refusing to download from non-regulator host '{host}'")
    dest = dest_dir / f"{slug(company)}-rhp.pdf"
    size = 0
    with httpx.stream("GET", url, headers={"User-Agent": UA}, follow_redirects=True, timeout=180) as r:
        r.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES:
                    fh.close(); dest.unlink(missing_ok=True)
                    raise RuntimeError(f"PDF larger than {MAX_BYTES >> 20} MB; skipped")
                fh.write(chunk)
    if dest.read_bytes()[:4] != b"%PDF":
        dest.unlink(missing_ok=True)
        raise RuntimeError("download is not a PDF")
    try:
        first = (PdfReader(str(dest)).pages[0].extract_text() or "").lower()
    except Exception as e:  # corrupt PDF
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"unreadable PDF: {e}")
    word = slug(company).split("-")[0]
    if "red herring prospectus" not in first or word not in re.sub(r"[^a-z0-9]+", "-", first):
        dest.unlink(missing_ok=True)
        raise RuntimeError("PDF is not the RHP for this company (cover page check failed)")
    return dest
