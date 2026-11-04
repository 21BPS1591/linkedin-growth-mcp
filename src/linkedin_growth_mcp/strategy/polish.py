"""Deterministic tidy-up applied to every draft before the safety gates (cheap models ignore formatting instructions)."""
from __future__ import annotations

import re

# a number with 3+ decimals, optionally followed by a crore marker: 803.629 Cr -> ₹804 crore
_LONG_DECIMAL = re.compile(r"(?<![\w.₹])(\d[\d,]*\.\d{3,})(?:\s*(?:Cr|cr|Crore|crore|crores)\b)?")
# any other plain amount followed by a crore marker: 150.0 Cr -> ₹150 crore
_PLAIN_CRORE = re.compile(r"(?<![\w₹.])(\d[\d,]*(?:\.\d+)?)\s*(?:Cr|cr|Crore|crores)\b")


_RUPEE_DECIMAL = re.compile(r"₹(\d[\d,]*\.\d+) ?(?:crore|cr)\b")


def _round(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.1f}".removesuffix(".0")


def polish(text: str) -> str:
    text = re.sub(r"\bRs\.?\s*₹\s*", "₹", text)  # 'Rs ₹657' -> '₹657'
    text = re.sub(r"\bRs\.?\s*(?=\d)", "₹", text)  # 'Rs 313' -> '₹313'
    def long_dec(m: re.Match) -> str:
        rounded = _round(float(m.group(1).replace(",", "")))
        return f"₹{rounded} crore" if re.search(r"(?i)cr", m.group(0)) else rounded

    def plain(m: re.Match) -> str:
        g = m.group(1)
        return f"₹{g.removesuffix('.0')} crore"

    text = _LONG_DECIMAL.sub(long_dec, text)
    text = _PLAIN_CRORE.sub(plain, text)
    # amounts already in ₹ ... crore form but with stray decimals (₹207.48 crore -> ₹207 crore; ₹12.34 crore -> ₹12.3 crore)
    return _RUPEE_DECIMAL.sub(lambda m: f"₹{_round(float(m.group(1).replace(',', '')))} crore", text)
