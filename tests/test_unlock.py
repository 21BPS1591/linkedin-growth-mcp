import time
from types import SimpleNamespace

from linkedin_growth_mcp import autopilot as ap


def _auto(rows, series_rows, kv):
    a = ap.Autopilot.__new__(ap.Autopilot)
    a.cfg = SimpleNamespace(pillar_series=("story", "bi_features"))
    log = []
    a.store = SimpleNamespace(
        get_kv=lambda k, d=None: kv.get(k, d), set_kv=lambda k, v: kv.__setitem__(k, v),
        latest_metrics=lambda: rows, series_metrics=lambda: series_rows, add_log=lambda *x, **y: log.append(x))
    a._alert = lambda *x: None
    return a, log


def _row(series, imp, ts):
    return {"series": series, "impressions": imp, "reactions": 3, "comments": 1, "reposts": 0, "published_at": ts}


def test_unlocks_after_five_good_posts():
    now = time.time()
    rows = [{"impressions": 200, "reactions": 1, "comments": 0, "reposts": 0, "published_at": now - 9999}] * 6
    kv = {"unlock_since": now - 100}
    a, _ = _auto(rows, [_row("story", 400, now)] * 5, kv)
    assert a.maybe_unlock_topic() == "product"
    assert kv["unlocked_series"] == ["product"]


def test_stays_locked_with_too_few_or_weak_posts():
    now = time.time()
    rows = [{"impressions": 400, "reactions": 1, "comments": 0, "reposts": 0, "published_at": now - 9999}] * 6
    a, _ = _auto(rows, [_row("story", 400, now)] * 4, {"unlock_since": now - 100})
    assert a.maybe_unlock_topic() is None
    a, log = _auto(rows, [dict(_row("story", 20, now), reactions=0, comments=0)] * 5, {"unlock_since": now - 100})
    assert a.maybe_unlock_topic() is None and log


def test_bar_follows_earlier_posts():
    now = time.time()
    strong = [{"impressions": 2000, "reactions": 80, "comments": 20, "reposts": 5, "published_at": now - 9999}] * 3
    a, _ = _auto(strong, [_row("story", 400, now)] * 5, {"unlock_since": now - 100})
    assert a.maybe_unlock_topic() is None  # a high-performing history raises the bar
