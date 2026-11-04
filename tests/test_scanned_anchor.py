from linkedin_growth_mcp.ipo import chittorgarh as cg

LINES = [
    "Sr. Name ofAnchorInvestor No.of No of Equity Bid",
    "1. NakshatraBharatVantageFund 6,83,600 13.40 150 10,25,40,000",
    "2. Radiant Global Fund-Class B Participating",
    "Shares 6,67,500 13.09 150 10,01,25,000",
    "3. Aarth AIF GrowthFund 6,10,246 9.25% 82 5,00,40,172",
    "VenturesFund",
    "19,61,346 100.00 25,27,05,172",
]


def test_scanned_letter_rows_wrap_and_total(monkeypatch):
    monkeypatch.setattr(cg, "_ocr_lines", lambda b: LINES)
    r = cg._parse_scanned_anchor_letter(b"")
    assert [x["shares"] for x in r["rows"]] == [683600, 667500, 610246]
    assert r["rows"][0]["name"] == "Nakshatra Bharat Vantage Fund"
    assert r["rows"][2]["name"].endswith("Ventures Fund")
    assert r["ok"] is True


def test_scanned_letter_rejected_when_total_mismatches(monkeypatch):
    monkeypatch.setattr(cg, "_ocr_lines", lambda b: LINES[:-1] + ["99,99,999 100.00 25,27,05,172"])
    assert cg._parse_scanned_anchor_letter(b"")["ok"] is False
