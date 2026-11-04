import time
from datetime import date, datetime, timedelta

import pytest

from linkedin_growth_mcp.service import Service
from linkedin_growth_mcp.store.db import Store
from test_core import make_cfg

from linkedin_growth_mcp.strategy import experiments as ex
from linkedin_growth_mcp.strategy.hooks import FEED_CUT, classify_hook, hook_report, hook_text, lint_hook


@pytest.fixture
def svc(tmp_path):
    cfg = make_cfg(tmp_path)
    return Service(cfg, Store(cfg.db_path))


def test_hook_text_cuts_at_word_within_feed_limit():
    t = hook_text("word " * 100)
    assert len(t) <= FEED_CUT and not t.endswith(" ")


def test_lint_flags_each_rule_book_problem():
    assert any("bare number" in i for i in lint_hook("6922 crore IPO opens today and it is big")["issues"])
    assert any("filler" in i for i in lint_hook("Excited to share my new analysis of an IPO")["issues"])
    assert any("dash" in i for i in lint_hook("Revenue grew \u2014 but profit fell sharply this year")["issues"])
    assert any("Link" in i for i in lint_hook("Read this at https://example.com before the IPO opens")["issues"])


def test_clean_hook_scores_high():
    r = lint_hook("Why did this company's profit fall while its revenue doubled?\n\nHere is what the filing shows.")
    assert r["score"] >= 80 and not r["fixes_needed"] and r["pattern"] == "question"


def test_classify_patterns():
    assert classify_hook("Why does this matter?") == "question"
    assert classify_hook("I was wrong about this IPO") == "confession"
    assert classify_hook("Last week a client asked me about GMP") == "story"
    assert classify_hook("Most people misread subscription numbers") == "contrarian"
    assert classify_hook("45% of the issue is a fresh issue") == "number_led"


def test_hook_report_refuses_to_name_a_winner_on_thin_data():
    rows = [{"text": "Why?", "impressions": 100, "reactions": 1, "comments": 0, "reposts": 0},
            {"text": "Most people are wrong", "impressions": 300, "reactions": 2, "comments": 1, "reposts": 0}]
    assert "Not enough data" in hook_report(rows)["verdict"]


D0 = date(2026, 11, 4)  # first day the trial may run (a Wednesday)


def _weekdays(n, start=D0):
    out, d = [], start
    while len(out) < n:
        if ex.is_post_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _trial():
    st = ex.new_state()
    ex.start_trial(st, D0, 1)
    return st["trial"]


def _mk(trial, specs):
    """specs: (format, slot, kind, impressions) per post, each on its own weekday."""
    posts = []
    for day, (f, sl, k, imp) in zip(_weekdays(len(specs)), specs):
        trial["days"][day.isoformat()] = {"format": f, "slot": sl}
        posts.append({"day": day.isoformat(), "kind": k, "impressions": imp})
    return posts


def test_trial_is_locked_before_cutoff_and_single():
    st = ex.new_state()
    with pytest.raises(ValueError, match="locked"):
        ex.start_trial(st, date(2026, 11, 3), 1)
    ex.start_trial(st, D0, 1)
    with pytest.raises(ValueError, match="already running"):
        ex.start_trial(st, date(2026, 11, 5), 1)


def test_post_days_skip_weekends_and_holidays():
    assert ex.is_post_day(date(2026, 11, 4))
    assert not ex.is_post_day(date(2026, 11, 7)) and not ex.is_post_day(date(2026, 11, 8))  # Sat, Sun
    assert not ex.is_post_day(date(2026, 11, 10))  # Diwali Balipratipada
    assert not ex.is_post_day(date(2026, 11, 5), ["2026-11-05"])  # owner-added skip date


def test_cells_are_balanced_and_repeatable():
    a, b = _trial(), _trial()
    cells = [ex.cell_for_day(a, d.isoformat()) for d in _weekdays(12)]
    assert cells == [ex.cell_for_day(b, d.isoformat()) for d in _weekdays(12)]
    counts = {}
    for c in cells:
        counts[(c["format"], c["slot"])] = counts.get((c["format"], c["slot"]), 0) + 1
    assert len(counts) == 6 and set(counts.values()) == {2}  # every format x slot cell used equally


def test_a_decided_factor_stops_varying():
    t = _trial()
    t["decided"]["format"] = {"value": "short", "verdict": "keep", "result": {}}
    cells = [ex.cell_for_day(t, d.isoformat()) for d in _weekdays(9)]
    assert {c["format"] for c in cells} == {"short"} and {c["slot"] for c in cells} == {9, 13, 18}


def test_post_kind_imbalance_does_not_fake_a_win():
    t = _trial()
    # pillar posts get 400 and IPO posts 100 in BOTH formats, but the short arm drew mostly pillar posts
    specs = [("short", 9, "pillar", 400)] * 5 + [("short", 9, "ipo", 100)] * 2 \
        + [("long", 9, "ipo", 100)] * 5 + [("long", 9, "pillar", 400)] * 2
    r = ex._eval_format([{**p, "format": s[0], "slot": s[1]} for p, s in zip(_mk(t, specs), specs)])
    assert r["verdict"] == "running" and abs(r["lift_pct"]) < 1  # raw means differ by ~70%, adjusted they do not


def test_one_viral_post_does_not_win():
    t = _trial()
    specs = [("short", 9, "pillar", v) for v in (200, 200, 200, 200, 5000)] + [("long", 9, "pillar", 200)] * 5
    r = ex._eval_format([{**p, "format": s[0], "slot": s[1]} for p, s in zip(_mk(t, specs), specs)])
    assert r["verdict"] == "running" and r["p"] > 0.2


def test_format_clear_win_clear_loss_and_unclear_at_the_limit():
    def run(short_vals, long_vals):
        t = _trial()
        specs = [("short", 9, "pillar", v) for v in short_vals] + [("long", 9, "pillar", v) for v in long_vals]
        return ex._eval_format([{**p, "format": s[0], "slot": s[1]} for p, s in zip(_mk(t, specs), specs)])

    assert run([600] * 5, [200] * 5)["verdict"] == "keep"
    loss = run([100] * 5, [200] * 5)
    assert loss["verdict"] == "revert" and "worse" in loss["note"]
    assert run([200, 220, 190, 210, 205], [205, 195, 215, 200, 210])["verdict"] == "running"
    final = run([200, 220, 190, 210, 205, 215, 195, 208, 202, 212], [205, 195, 215, 200, 210, 198, 207, 203, 211, 199])
    assert final["verdict"] == "revert" and final["value"] == "long" and "old format stays" in final["note"]
    assert run([600] * 4, [200] * 5)["verdict"] == "running"  # fewer than 5 in an arm: no verdict


def test_slot_pins_a_clearly_better_hour_else_keeps_nine():
    def run(per_slot, n):
        t = _trial()
        specs = []
        for i in range(n):
            for sl in (9, 13, 18):
                specs.append(("long" if i % 2 else "short", sl, "ipo" if i % 2 else "pillar", per_slot[sl][i]))
        return ex._eval_slot([{**p, "format": s[0], "slot": s[1]} for p, s in zip(_mk(t, specs), specs)])

    clear = run({9: [200] * 6, 13: [210] * 6, 18: [800] * 6}, 6)
    assert clear["verdict"] == "keep" and clear["value"] == 18
    noisy = run({9: [200, 220, 190, 210, 205, 195, 215, 200], 13: [205, 190, 215, 200, 210, 198, 202, 207],
                 18: [199, 211, 204, 196, 208, 201, 193, 209]}, 8)
    assert noisy["verdict"] == "revert" and noisy["value"] == 9


def test_decide_freezes_only_final_factors_and_finish_needs_both():
    st = ex.new_state()
    ex.start_trial(st, D0, 1)
    t = st["trial"]
    res = {"format": {"verdict": "keep", "value": "short", "note": "n"}, "slot": {"verdict": "running", "note": "n"}}
    assert ex.decide(t, res, D0) == ["format"] and t["decided"]["format"]["value"] == "short"
    assert ex.finish(st, D0) is False
    res["slot"] = {"verdict": "revert", "value": 9, "note": "n"}
    assert ex.decide(t, res, D0) == ["slot"] and ex.finish(st, D0) is True
    assert st["trial"] is None and st["history"][0]["decided"]["slot"]["value"] == 9


# --- the autopilot: weekdays only, and the trial running by itself ---
def _post_on(svc, day, kind, impressions):
    d = svc.store.add_draft("listicle", "question", f"Why does this matter on {day}?\n\nBody text here.", None)
    if kind == "ipo":
        iid = svc.store.add_idea("ipo", f"IPO {day}", "brief")
        svc.store.update_idea(iid, status="drafted", draft_id=d)
    pid = svc.store.add_post(d, f"urn:{d}", None)
    svc.store.conn.execute("UPDATE posts SET published_at=? WHERE id=?",
                           (datetime(day.year, day.month, day.day, 10, 0).timestamp(), pid))
    svc.store.conn.commit()
    svc.store.save_metrics(pid, "72h", impressions, 1, 0, 0)


def test_autopilot_posts_on_weekdays_only(svc, monkeypatch):
    calls = []
    monkeypatch.setattr(svc.autopilot, "_active", lambda today: calls.append(today) or [])
    assert svc.autopilot.next_candidate(date(2026, 11, 7)) is None and calls == []  # Saturday: not even looked at
    svc.autopilot.next_candidate(date(2026, 11, 4))
    assert calls == [date(2026, 11, 4)]
    svc.store.set_kv("skip_dates", ["2026-11-05"])
    assert svc.autopilot.is_post_day(date(2026, 11, 5)) is False


def test_trial_is_locked_before_cutoff_in_the_autopilot(svc):
    assert svc.autopilot.auto_experiments_if_due(date(2026, 11, 3)) is None
    assert svc.store.get_kv("experiments") is None and svc.autopilot.trial_cell(date(2026, 11, 3)) is None


def test_trial_starts_at_the_cutoff_without_a_baseline_and_skips_weekends(svc):
    svc.autopilot.auto_experiments_if_due(D0)
    trial = svc.store.get_kv("experiments")["trial"]
    assert trial["started"] == "2026-11-04" and svc.autopilot.trial_cell(D0)["slot"] in (9, 13, 18)
    assert svc.autopilot.trial_cell(date(2026, 11, 7)) is None and "2026-11-07" not in svc.store.get_kv("experiments")["trial"]["days"]
    assert svc.autopilot.trial_cell(date(2026, 11, 10)) is None  # holiday


def test_trial_decides_both_factors_by_itself_and_pins_the_winners(svc):
    base = {"short": 2.0, "long": 1.0}
    slot_mult = {9: 1.0, 13: 1.0, 18: 2.5}
    for day in _weekdays(45):
        svc.autopilot.auto_experiments_if_due(day)
        cell = svc.autopilot.trial_cell(day)
        if cell is None:
            break
        _post_on(svc, day, "ipo" if day.toordinal() % 3 == 0 else "pillar", int(200 * base[cell["format"]] * slot_mult[cell["slot"]]))
        if svc.store.get_kv("experiments")["trial"] is None:
            break
    st = svc.store.get_kv("experiments")
    if st["trial"] is not None:  # the last day's posts are judged on the next call
        svc.autopilot.auto_experiments_if_due(_weekdays(46)[-1])
        st = svc.store.get_kv("experiments")
    assert st["trial"] is None, "trial did not finish within 45 weekdays"
    assert svc.store.get_kv("format_mode") == "short" and svc.store.get_kv("post_hours") == [18]
    logged = {r["action"] for r in svc.store.conn.execute("SELECT action FROM autopilot_log")}
    assert {"experiment_started", "experiment_result", "experiment_finished"} <= logged
    assert svc.autopilot.slot_hours() == (18,)


def test_trial_off_switch(svc):
    object.__setattr__(svc.cfg, "auto_experiments", False)
    assert svc.autopilot.auto_experiments_if_due(date(2026, 11, 10)) is None
    assert svc.store.get_kv("experiments") is None
