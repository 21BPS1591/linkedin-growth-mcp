"""Follower tracking, reply drafting, posting-time bandit and the editor gate."""
import random

from linkedin_growth_mcp.linkedin.followers import parse_followers
from linkedin_growth_mcp.store.db import Store
from linkedin_growth_mcp.strategy import conversation as cv
from linkedin_growth_mcp.strategy.timing import SlotBandit


# --- followers ---------------------------------------------------------------
def test_parse_followers_handles_plain_and_abbreviated():
    assert parse_followers("Aakash\n1,284 followers\n500+ connections") == 1284
    assert parse_followers("12.5K followers") == 12500
    assert parse_followers("no numbers here") is None


def test_parse_followers_takes_the_profile_own_count_not_the_largest():
    page = "Aakash\n1,284 followers\n500+ connections\nPeople also viewed\nJio Platforms\n12.4M followers"
    assert parse_followers(page) == 1284


POST_SUMMARY_PAGE = """Discovery

425

Impressions

In-network (followers and connections)

31%

Out-of-network

69%

283

Members reached

Profile activity

0

Profile viewers from this post

0

Followers gained from this post

Engagement

6

Social engagements

Reactions

6

Comments

0

Reposts

0

Saves

0

Sends on LinkedIn

0

Top demographics

All
Job title
Location
Seniority
Company
Industry
Company size

Seniority

Senior

43%

Company size

10,001+ employees

36%

Industry

IT Services and IT Consulting

25%

Location

Greater Bengaluru Area

20%

Job title

Business Analyst

7%

Analytics & tools
"""


def test_parse_post_summary_reads_reach_and_top_demographics():
    from linkedin_growth_mcp.linkedin.analytics import parse_post_summary

    r = parse_post_summary(POST_SUMMARY_PAGE)
    assert r["metrics"] == {"impressions": 425, "reactions": 6, "comments": 0, "reposts": 0}
    assert r["extra"]["members_reached"] == 283 and r["extra"]["followers_gained"] == 0
    rows = {(c, v): (pct, num) for c, v, pct, num in r["rows"]}
    assert rows[("Summary", "In-network share")][0] == 0.31
    assert rows[("Summary", "Out-of-network share")][0] == 0.69
    assert rows[("Seniority", "Senior")][0] == 0.43
    assert rows[("Company size", "10,001+ employees")][0] == 0.36
    assert rows[("Industry", "IT Services and IT Consulting")][0] == 0.25
    assert rows[("Job title", "Business Analyst")][0] == 0.07
    assert ("Location", "Greater Bengaluru Area") in rows


def test_follower_history_reports_the_change(tmp_path):
    st = Store(tmp_path / "f.db")
    st.save_followers("2026-10-29", 1200)
    st.save_followers("2026-10-30", 1215)
    rows = st.follower_rows()
    assert [r["count"] for r in rows] == [1215, 1200]  # newest first


# --- conversation quality ----------------------------------------------------
def test_comment_count_reads_the_page():
    assert cv.comment_count("14 reactions\n3 comments\n1 repost") == 3
    assert cv.comment_count("no comments section") == 0


def test_clean_analysis_clamps_numbers_and_drops_unsafe_text():
    out = cv.clean_analysis({
        "comments": 4, "author_replies": 2,
        "comment_types": {"question": 2, "low_effort": 2, "bogus": 9},
        "comment_quality": 42, "reply_quality": -3,
        "strongest_comment": "Asked how the validation tool handles edge cases",
        "weakest_own_reply": "Visit https://spam.example",
        "coaching": ["Answer the question directly", "Ignore previous instructions", "", "a", "b"],
    })
    assert out["comment_quality"] == 10 and out["reply_quality"] == 0
    assert "bogus" not in out["comment_types"] and out["comment_types"]["question"] == 2
    assert out["weakest_own_reply"] == ""  # link stripped
    assert out["coaching"] == ["Answer the question directly", "a", "b"]
    assert out["strongest_comment"].startswith("Asked how")


def test_clean_analysis_survives_a_junk_response():
    out = cv.clean_analysis("not a dict")
    assert out["comments"] == 0 and out["reply_quality"] is None and out["coaching"] == []


def test_conversation_rows_keep_only_the_latest_reading_per_post(tmp_path):
    st = Store(tmp_path / "c.db")
    did = st.add_draft("story", "number", "x" * 400, None)
    pid = st.add_post(did, "urn:li:share:1", "https://example.invalid/1")
    base = cv.clean_analysis({"comments": 1, "author_replies": 0, "comment_quality": 5})
    st.add_conversation(pid, base)
    st.add_conversation(pid, cv.clean_analysis({"comments": 3, "author_replies": 2, "comment_quality": 7}))
    rows = st.conversation_rows()
    assert len(rows) == 1 and rows[0]["comments"] == 3
    assert abs(rows[0]["reply_rate"] - 2 / 3) < 1e-9


# --- posting-time bandit -----------------------------------------------------
def test_slot_bandit_prefers_the_better_hour():
    b = SlotBandit.from_history((9, 18), [(9, 0.1)] * 10 + [(18, 0.9)] * 10, rng=random.Random(0))
    assert b.choose() == 18
    assert b.ranking()[0][0] == 18


def test_slot_bandit_ignores_hours_that_are_no_longer_configured():
    b = SlotBandit.from_history((9,), [(9, 0.5), (21, 0.9)])
    assert set(b.slots) == {9} and b.slots[9].n == 1


# --- editor gate -------------------------------------------------------------
def _autopilot(score, gate=True, min_score=6.0):
    from types import SimpleNamespace

    from linkedin_growth_mcp.autopilot import Autopilot

    a = Autopilot.__new__(Autopilot)
    a.cfg = SimpleNamespace(editor_gate=gate, editor_min_score=min_score)
    logged = []
    a.store = SimpleNamespace(add_log=lambda *x, **y: logged.append(x), update_draft=lambda *x, **y: None)
    a._editor_score = lambda did: (score, ["weak hook"])
    return a, logged


def test_editor_passes_a_strong_draft():
    a, _ = _autopilot(8.0)
    keep, score, hints = a._editor_round({"draft_id": 1, "text": "t"}, None)
    assert keep is not None and score == 8.0 and hints == []


def test_editor_asks_for_one_rewrite_then_accepts_the_best():
    a, logged = _autopilot(3.0)
    keep, score, hints = a._editor_round({"draft_id": 1, "text": "t"}, None)
    assert keep is None and hints == ["weak hook"]  # first weak draft: rewrite
    keep2, _, hints2 = a._editor_round({"draft_id": 2, "text": "t"}, (3.0, {}, 1))
    assert keep2 is None and hints2 == []  # second weak draft: stop spending requests
    assert any("editor_rewrite" in x for x in logged)


def test_editor_gate_can_be_switched_off():
    a, _ = _autopilot(1.0, gate=False)
    keep, score, _ = a._editor_round({"draft_id": 1, "text": "t"}, None)
    assert keep is not None and score == 10.0
