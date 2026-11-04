"""SEBI public-issues listing: the regulator's own index of RHP filings (mainboard IPOs only)."""
from __future__ import annotations

import re

import httpx

LISTING_URL = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=11"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
# <a ... href=".../company-rhp_105380.html" ... title="Company - RHP <br><a href='...abridged.pdf'>
ROW = re.compile(r'<a[^>]+href="([^"]+_\d+\.html)"[^>]*title="([^"<]+?)\s*<br>', re.IGNORECASE)
NOT_PLAIN_RHP = re.compile(r"addendum|corrigendum|drhp|draft|price band|abridged|advertisement", re.IGNORECASE)
VIEWER_PDF = re.compile(r"file=(https://www\.sebi\.gov\.in/[^'\"> ]+?\.pdf)", re.IGNORECASE)


def parse_listing(html: str) -> list[dict[str, str]]:
    out, seen = [], set()
    for url, title in ROW.findall(html):
        title = re.sub(r"\s+", " ", title).strip()
        if not re.search(r"\bRHP\b", title) or NOT_PLAIN_RHP.search(title) or url in seen:
            continue
        company = re.split(r"\s*[-–—:�]+\s*RHP\b", title, maxsplit=1)[0].strip(" -–—:�")
        if company:
            seen.add(url)
            out.append({"company": company, "filing_url": url})
    return out


def resolve_pdf(filing_url: str) -> str:
    """The filing page embeds the full RHP in a viewer iframe (?file=<pdf>)."""
    r = httpx.get(filing_url, headers={"User-Agent": UA}, follow_redirects=True, timeout=60)
    r.raise_for_status()
    m = VIEWER_PDF.search(r.text)
    if not m:
        raise RuntimeError("no PDF found on the SEBI filing page")
    return m.group(1)


def latest_rhps() -> list[dict[str, str]]:
    r = httpx.get(LISTING_URL, headers={"User-Agent": UA}, follow_redirects=True, timeout=60)
    r.raise_for_status()
    return parse_listing(r.text)
