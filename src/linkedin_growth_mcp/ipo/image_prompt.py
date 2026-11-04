"""Image prompt for an IPO post: a clean week-strip calendar with the real open/close dates spelled out.

Image models draw calendars badly when left to guess, so every day is listed explicitly with its role.
"""
from __future__ import annotations

from datetime import date, timedelta

from .discovery import slug


def image_filename(company: str) -> str:
    """Name to save the finished image under (in the images folder) so the autopilot attaches it."""
    return f"{slug(company)}.png"


def _title(company: str) -> str:
    """Readable company name for the graphic (RHP covers are SHOUTING)."""
    small = {"and", "of", "the"}
    suffix = {"ltd": "Ltd", "limited": "Ltd", "pvt": "Pvt", "private": "Pvt", "inc": "Inc", "llp": "LLP"}
    words = []
    for w in company.replace(".", " ").split():
        lw = w.lower()
        if lw in suffix:
            words.append(suffix[lw])
        elif lw in small:
            words.append(lw)
        elif w.isupper() and len(w) <= 3:  # short all-caps words (SFC, BLS, HD) are acronyms
            words.append(w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def calendar_prompt(company: str, opens: date, closes: date) -> str:
    start = opens - timedelta(days=opens.weekday())  # Monday of the opening week
    end = closes + timedelta(days=6 - closes.weekday())  # Sunday of the closing week
    cells = []
    day = start
    while day <= end:
        if day == opens:
            role = "highlighted in GREEN with the small label 'Opens'"
        elif day == closes:
            role = "highlighted in ORANGE with the small label 'Closes'"
        elif opens < day < closes:
            role = "light green tint (issue is open)"
        else:
            role = "plain light grey, no label"
        cells.append(f"- {day:%a %d %b}: {role}")
        day += timedelta(days=1)
    return (
        "Create a clean, modern LinkedIn graphic, landscape 1200x627 px, styled like a wall calendar.\n"
        f"Heading (large, bold, left aligned): \"{_title(company)} IPO\".\n"
        "Subheading (smaller): \"Bid dates\".\n"
        f"Below it, a calendar grid with weekday headers Mon Tue Wed Thu Fri Sat Sun and exactly "
        f"{(end - start).days // 7 + 1} week row(s). Use EXACTLY these dates and roles, in order, no other dates:\n"
        + "\n".join(cells)
        + f"\nShow the month name(s) above the grid ({start:%B %Y}"
        + (f" and {end:%B %Y}" if (end.month, end.year) != (start.month, start.year) else "")
        + ").\n"
        "Style: white background, navy text, teal accent, rounded date squares, plenty of whitespace, a clean "
        "sans-serif font (Inter or Helvetica look). Small footer text: \"Source: RHP filed with SEBI. Educational "
        "analysis, not investment advice.\"\n"
        "Do NOT add logos, stock photos, charts, prices, price bands, or any text beyond what is listed above. "
        "Double-check the weekday of every date before finalising."
    )
