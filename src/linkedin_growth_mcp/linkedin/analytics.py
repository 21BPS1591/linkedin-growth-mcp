"""Post analytics reader. All page-specific knowledge lives in this file so breakage is easy to fix."""
from __future__ import annotations

import csv
import io
import re
from typing import Any

from .browser import LinkedInBrowser

_NUM = r"(\d[\d,\.]*\s*[KkMm]?)"


def _to_int(s: str) -> int:
    s = s.strip().replace(",", "")
    mult = 1
    if s and s[-1] in "kK":
        mult, s = 1_000, s[:-1]
    elif s and s[-1] in "mM":
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _find(text: str, label: str, number_first: bool, gap: str = r"[ \t]*\n?[ \t]*") -> int:
    pat = _NUM + gap + label if number_first else label + gap + _NUM
    m = re.search(pat, text, re.IGNORECASE)
    return _to_int(m.group(1)) if m else 0


def parse_analytics_text(text: str) -> dict[str, int]:
    # LinkedIn renders either "1,234\nImpressions" or "Impressions\n1,234"; pick one layout
    # per page (decided by the Impressions line) so adjacent labels don't steal each other's numbers.
    number_first = bool(re.search(_NUM + r"[ \t]*\n[ \t]*Impressions", text, re.IGNORECASE))
    # The post-summary page shows "99 / Impressions" number-first, but its Engagement block
    # ("1 / Social engagements", then "Reactions / 1", "Comments / 0", ...) label-first.
    # Reading those labels number-first would hand each metric its neighbour's value.
    eng = re.search(r"Social\s+engagements", text, re.IGNORECASE)
    if eng:
        block = text[eng.end():]
        return {
            "impressions": _find(text, "Impressions", number_first),
            "reactions": _find(block, "Reactions", False, r"\s*"),
            "comments": _find(block, "Comments?", False, r"\s*"),
            "reposts": _find(block, "Reposts?", False, r"\s*"),
        }
    return {
        "impressions": _find(text, "Impressions", number_first),
        "reactions": _find(text, "Reactions", number_first),
        "comments": _find(text, "Comments?", number_first),
        "reposts": _find(text, "Reposts?", number_first),
    }


_DEMO = re.compile(r"^(Job title|Location|Seniority|Company size|Company|Industry)[ \t]*\n+[ \t]*(.+?)[ \t]*\n+[ \t]*(\d+(?:\.\d+)?)%",
                   re.MULTILINE)


def _pct_after(text: str, label: str) -> float | None:
    m = re.search(label + r"[^\n]*\n+\s*(\d+(?:\.\d+)?)%", text, re.IGNORECASE)
    return float(m.group(1)) / 100 if m else None


def parse_post_summary(text: str) -> dict[str, Any]:
    """Whole post-summary page -> headline metrics, reach/profile numbers and the 'Top demographics' block.

    Summary rows are (category, value, pct, num) ready for Store.save_audience; demographics hold the single top item per category."""
    metrics = parse_analytics_text(text)
    extra = {
        "members_reached": _find(text, "Members reached", True, r"[ \t]*\n+[ \t]*"),
        "profile_viewers": _find(text, "Profile viewers from this post", True, r"[ \t]*\n+[ \t]*"),
        "followers_gained": _find(text, "Followers gained from this post", True, r"[ \t]*\n+[ \t]*"),
        "saves": _find(text, "Saves", False, r"\s*"),
        "sends": _find(text, "Sends on LinkedIn", False, r"\s*"),
    }
    rows: list[tuple[str, str, float | None, float | None]] = [
        ("Summary", "Members reached", None, float(extra["members_reached"])),
        ("Summary", "Profile viewers", None, float(extra["profile_viewers"])),
        ("Summary", "Followers gained", None, float(extra["followers_gained"])),
        ("Summary", "Saves", None, float(extra["saves"])),
        ("Summary", "Sends on LinkedIn", None, float(extra["sends"])),
    ]
    for label, key in (("In-network", "In-network share"), ("Out-of-network", "Out-of-network share")):
        p = _pct_after(text, label)
        if p is not None:
            rows.append(("Summary", key, p, round(p * 100, 1)))  # audience_report shows Summary rows by `num`
    demo = text.split("Top demographics", 1)[1] if "Top demographics" in text else ""
    demo = demo.split("Analytics & tools", 1)[0]
    for cat, val, pct in _DEMO.findall(demo):
        rows.append((cat, val, float(pct) / 100, None))
    return {"metrics": metrics, "extra": extra, "rows": rows}


async def scrape_post_summary(browser: LinkedInBrowser, urn: str) -> dict[str, Any]:
    text = await browser.read_text(f"https://www.linkedin.com/analytics/post-summary/{urn}/", scroll=6)
    out = parse_post_summary(text)
    if not any(out["metrics"].values()):
        raise RuntimeError("Could not parse analytics (page layout may have changed, or the post was deleted).")
    return out


async def scrape_post_metrics(browser: LinkedInBrowser, urn: str) -> dict[str, int]:
    text = await browser.read_text(f"https://www.linkedin.com/analytics/post-summary/{urn}/", scroll=1)
    metrics = parse_analytics_text(text)
    if not any(metrics.values()):
        raise RuntimeError(
            "Could not parse analytics (page layout may have changed). Use record_metrics to enter numbers manually."
        )
    return metrics


def parse_export_csv(content: str) -> list[dict[str, Any]]:
    """Parse LinkedIn's content-analytics CSV export rows (header names matched case-insensitively)."""
    rows = []
    for r in csv.DictReader(io.StringIO(content)):
        low = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        url = next((v for k, v in low.items() if "url" in k), "")
        rows.append(
            {
                "url": url,
                "impressions": _to_int(low.get("impressions", "0")),
                "reactions": _to_int(low.get("reactions", low.get("likes", "0"))),
                "comments": _to_int(low.get("comments", "0")),
                "reposts": _to_int(low.get("reposts", "0")),
            }
        )
    return rows


def _single_post_row(ws) -> dict[str, Any] | None:
    """LinkedIn's single-post export is a vertical sheet: label in column A, value in column B."""
    kv: dict[str, str] = {}
    for raw in ws.iter_rows(values_only=True):
        if len(raw) >= 2 and raw[0] is not None and raw[1] is not None:
            kv.setdefault(str(raw[0]).strip().lower(), str(raw[1]).strip())
    if "post url" not in kv or "impressions" not in kv:
        return None
    return {
        "url": kv["post url"],
        "impressions": _to_int(kv["impressions"]),
        "reactions": _to_int(kv.get("reactions", "0")),
        "comments": _to_int(kv.get("comments", "0")),
        "reposts": _to_int(kv.get("reposts", "0")),
    }


def parse_export_xlsx(path: str) -> list[dict[str, Any]]:
    """Read LinkedIn's .xlsx content export. Any sheet whose header row has a URL and an Impressions column is used,
    wherever that header sits (the export puts title rows above it)."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    rows: list[dict[str, Any]] = []
    for ws in wb.worksheets:
        kv = _single_post_row(ws)
        if kv:
            rows.append(kv)
            continue
        header: list[str] | None = None
        for raw in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in raw]
            low = [c.lower() for c in cells]
            if header is None:
                if any("url" in c for c in low) and any("impressions" in c for c in low):
                    header = low
                continue
            if not any(cells):
                continue
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(header)
            w.writerow(cells)
            rows.extend(parse_export_csv(buf.getvalue()))
    wb.close()
    return rows


def parse_audience_xlsx(path: str) -> dict[str, Any]:
    """Single-post export -> post url, headline numbers and the demographics block."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    url, nums, demo, in_demo = "", {}, [], False
    for raw in ws.iter_rows(values_only=True):
        cells = [None if c is None else str(c).strip() for c in raw] + [None, None, None]
        a, b, c = cells[0], cells[1], cells[2]
        if not a:
            continue
        if a.lower() == "post url":
            url = b or ""
        elif a.lower() == "category":
            in_demo = True
        elif in_demo and b:
            try:
                demo.append((a, b, float((c or "").rstrip("%")) / 100 if c else None))
            except ValueError:
                demo.append((a, b, None))
        elif a.lower() in ("impressions", "members reached") and b:
            nums[a.lower()] = _to_int(b)
    wb.close()
    return {"url": url, "impressions": nums.get("impressions", 0), "members_reached": nums.get("members reached", 0), "demographics": demo}
