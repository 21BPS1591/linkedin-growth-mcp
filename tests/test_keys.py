import json

import pytest
from google.genai import errors

from linkedin_growth_mcp.gemini import retry
from linkedin_growth_mcp.gemini.image import GeminiImage
from linkedin_growth_mcp.gemini.keys import KeyMissing, describe
from linkedin_growth_mcp.service import Service
from linkedin_growth_mcp.store.db import Store
from test_core import make_cfg


def _svc(tmp_path, **keys):
    cfg = make_cfg(tmp_path)
    for k, v in keys.items():
        object.__setattr__(cfg, k, v)
    return Service(cfg, Store(cfg.db_path))


def test_image_generation_never_falls_back_to_the_content_key(tmp_path):
    svc = _svc(tmp_path, gemini_api_key="content-key")  # a content key, but no image key
    with pytest.raises(KeyMissing, match="GEMINI_IMAGE_API_KEY"):
        GeminiImage(svc.cfg, svc.store)


def test_insights_and_extraction_work_pauses_without_their_own_keys(tmp_path):
    svc = _svc(tmp_path, gemini_api_key="content-key")
    with pytest.raises(KeyMissing, match="GEMINI_INSIGHTS_API_KEY"):
        svc.insights_text
    with pytest.raises(KeyMissing, match="GEMINI_EXTRACTION_API_KEY"):
        svc.gemini_client("extraction")
    with pytest.raises(KeyMissing):
        svc.spend("insights")
    assert svc.store.usage_today(svc.cfg.usage_kind("content")) == 0  # nothing was charged to the content key


def test_each_key_counts_against_its_own_cap(tmp_path):
    svc = _svc(tmp_path, gemini_api_key="c", gemini_extraction_api_key="e", daily_extraction_call_cap=2)
    svc.spend("extraction")
    svc.spend("extraction")
    with pytest.raises(RuntimeError, match="cap"):
        svc.spend("extraction")
    assert svc.store.usage_today(svc.cfg.usage_kind("content")) == 0 and svc.store.usage_today(svc.cfg.usage_kind("extraction")) == 2
    assert len({svc.cfg.usage_kind(p) for p in ("content", "image", "extraction", "insights")}) == 4


def test_report_names_the_accounts_and_never_leaks_a_key(tmp_path):
    svc = _svc(tmp_path, gemini_api_key="SECRET-CONTENT", gemini_account="a@x.com", gemini_image_api_key="SECRET-IMAGE",
               gemini_image_account="b@x.com")
    rep = svc.api_keys_report()
    text = json.dumps(rep)
    assert "SECRET" not in text and "a@x.com" in text and "b@x.com" in text
    assert rep["paused"] == ["extraction", "insights"]
    assert "SECRET" not in json.dumps(describe(svc.cfg, svc.store))


def test_paused_jobs_skip_quietly_and_log_once_a_day(tmp_path, monkeypatch):
    svc = _svc(tmp_path, gemini_api_key="c")
    from linkedin_growth_mcp.ipo import sebi

    monkeypatch.setattr(sebi, "latest_rhps", lambda: (_ for _ in ()).throw(AssertionError("must not fetch filings without a key")))
    assert svc.autopilot.sync_filings()["paused"] == "extraction key missing"
    assert svc.autopilot.watch_comments() is None
    svc.autopilot._learn_from_comments()
    svc.autopilot.sync_filings()
    pending = [r["detail"] for r in svc.store.conn.execute("SELECT detail FROM autopilot_log WHERE action='key_pending'")]
    assert len(pending) == 2 and any("GEMINI_EXTRACTION_API_KEY" in p for p in pending) and any("GEMINI_INSIGHTS_API_KEY" in p for p in pending)


def test_quota_errors_are_not_retried_but_overload_is(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda s: None)
    calls = []

    def quota():
        calls.append(1)
        raise errors.ClientError(429, {"error": {"message": "quota"}})

    with pytest.raises(errors.APIError):
        retry.with_retry(quota)
    assert len(calls) == 1  # a spent quota is never hammered

    calls.clear()

    def overloaded():
        calls.append(1)
        raise errors.ServerError(503, {"error": {"message": "busy"}})

    with pytest.raises(errors.APIError):
        retry.with_retry(overloaded)
    assert len(calls) == 3  # transient overload gets at most two gentle retries


def test_per_minute_guard_waits_instead_of_bursting(monkeypatch):
    from linkedin_growth_mcp.gemini import keys

    clock = {"t": 1000.0}
    slept = []
    monkeypatch.setattr(keys.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(keys.time, "sleep", lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)))
    keys._calls.pop("k-test", None)
    for _ in range(keys.RPM_GUARD):
        keys.throttle("k-test")
    assert slept == []  # up to 12 calls a minute go straight through (provider limit is 15)
    keys.throttle("k-test")
    assert slept and 59 <= sum(slept) <= 61  # the 13th waits for the oldest call to leave the minute


def test_image_generation_is_off_while_its_cap_is_zero(tmp_path):
    svc = _svc(tmp_path, gemini_image_api_key="img", daily_image_call_cap=0)
    with pytest.raises(RuntimeError, match="switched off"):
        GeminiImage(svc.cfg, svc.store)
    assert [k["enabled"] for k in describe(svc.cfg) if k["purpose"] == "image"] == [False]
    assert svc.store.usage_today(svc.cfg.usage_kind("image")) == 0  # nothing was counted or sent


# --- content failover: the image account's key backs up CONTENT generation only ---
class _Reply:
    def __init__(self, text):
        self.text = text


class _Fake:
    """A stand-in Gemini client that records calls and can refuse with a quota error."""

    def __init__(self, name, log, fail_429=False):
        self.name, self.log, self.fail_429 = name, log, fail_429
        self.models = self

    def generate_content(self, **kw):
        self.log.append(self.name)
        if self.fail_429:
            raise errors.ClientError(429, {"error": {"message": "quota"}})
        return _Reply("{}")


def _content_svc(tmp_path, **extra):
    svc = _svc(tmp_path, gemini_api_key="content", gemini_account="a@x.com", gemini_image_api_key="image",
               gemini_image_account="b@x.com", gemini_content_backup="image", daily_gemini_call_cap=2, **extra)
    return svc


def test_content_uses_the_backup_key_only_after_the_content_cap(tmp_path, monkeypatch):
    svc = _content_svc(tmp_path)
    t, log = svc.text, []
    t.client = _Fake("content", log)
    monkeypatch.setattr(t, "_backup", lambda: _Fake("backup", log))
    for _ in range(3):
        t._json("hi")
    assert log == ["content", "content", "backup"]  # the cap is 2, so the third request moves to the backup
    assert svc.store.usage_today(svc.cfg.usage_kind("content")) == 2
    assert svc.store.usage_today(svc.cfg.content_backup_kind()) == 1
    assert svc.store.usage_today(svc.cfg.usage_kind("image")) == 0  # the image counter is untouched
    assert [r["action"] for r in svc.store.conn.execute("SELECT action FROM autopilot_log WHERE action='key_failover'")] == ["key_failover"]


def test_a_google_quota_refusal_moves_content_to_the_backup_without_retrying(tmp_path, monkeypatch):
    svc = _content_svc(tmp_path)
    t, log = svc.text, []
    t.client = _Fake("content", log, fail_429=True)
    monkeypatch.setattr(t, "_backup", lambda: _Fake("backup", log))
    t._json("hi")
    assert log == ["content", "backup"]  # one refused attempt, never hammered, then the backup


def test_the_backup_has_its_own_cap_and_nothing_else_fails_over(tmp_path, monkeypatch):
    svc = _content_svc(tmp_path, daily_content_backup_cap=1)
    t, log = svc.text, []
    t.client = _Fake("content", log)
    monkeypatch.setattr(t, "_backup", lambda: _Fake("backup", log))
    for _ in range(3):
        t._json("hi")  # 2 on the content key, 1 on the backup
    with pytest.raises(RuntimeError, match="cap"):
        t._json("hi")  # backup cap (1) reached as well: stop, do not touch any other key
    assert log == ["content", "content", "backup"]
    (tmp_path / "i").mkdir()
    insights = _svc(tmp_path / "i", gemini_api_key="content", gemini_image_api_key="image", gemini_content_backup="image",
                    gemini_insights_api_key="ins", daily_insights_call_cap=1)
    it, ilog = insights.insights_text, []
    it.client = _Fake("insights", ilog)
    it._json("hi")
    with pytest.raises(RuntimeError, match="cap"):
        it._json("hi")  # insights never fails over to anything
    assert ilog == ["insights"]


def test_no_backup_configured_means_no_failover(tmp_path):
    svc = _svc(tmp_path, gemini_api_key="content", gemini_image_api_key="image", daily_gemini_call_cap=1)
    t, log = svc.text, []
    t.client = _Fake("content", log)
    t._json("hi")
    with pytest.raises(RuntimeError, match="cap"):
        t._json("hi")
    assert log == ["content"]


def test_report_shows_the_backup_row_without_a_key(tmp_path):
    svc = _content_svc(tmp_path)
    rows = {k["purpose"]: k for k in svc.api_keys_report()["keys"]}
    assert rows["content_backup"]["account"] == "b@x.com" and rows["content_backup"]["env_var"] == "GEMINI_IMAGE_API_KEY"


# --- model routing: the stronger model first, the light model as fallback on the SAME key ---
class _ModelFake:
    def __init__(self, log, refuse=()):
        self.log, self.refuse, self.models = log, set(refuse), self

    def generate_content(self, model, **kw):
        self.log.append(model)
        if model in self.refuse:
            raise errors.ClientError(429, {"error": {"message": "quota"}})
        return _Reply("{}")


def test_a_refused_strong_model_falls_back_to_the_light_model_on_the_same_key(tmp_path):
    svc = _svc(tmp_path, gemini_api_key="content", gemini_draft_model="big", gemini_draft_fallback_model="lite")
    t, log = svc.text, []
    t.client = _ModelFake(log, refuse={"big"})
    t._json("hi")
    assert log == ["big", "lite"]
    assert svc.store.usage_today(svc.cfg.usage_kind("content")) == 2  # both requests are counted on the content key
    assert svc.store.usage_today(svc.cfg.content_backup_kind()) == 0   # no backup key was needed


def test_editor_and_other_jobs_use_the_light_model_and_never_the_writer_model(tmp_path):
    svc = _svc(tmp_path, gemini_api_key="content", gemini_insights_api_key="ins", gemini_draft_model="big",
               gemini_draft_fallback_model="lite")
    log = []
    t = svc.text
    t.client = _ModelFake(log)
    t.review("a post", None, None, None, None)
    t.suggest_hooks("a post", "")
    it = svc.insights_text
    it.client = _ModelFake(log)
    it._json("hi")
    assert log == ["m", "m", "m"]  # make_cfg's text model is "m": none of these touched the writer model "big"


def test_a_draft_without_facts_may_not_invent_personal_experience(tmp_path):
    svc = _svc(tmp_path, gemini_api_key="content")
    seen = []
    svc.text._json = lambda prompt: seen.append(prompt) or {"text": "x", "image_prompt": None}
    svc.text.draft_post({"raw_text": "p"}, {}, "how_to", "question", [], [], {"title": "t", "brief": "b", "facts": ""}, None)
    svc.text.draft_post({"raw_text": "p"}, {}, "case_study", "question", [], [], {"title": "t", "brief": "b", "facts": "Revenue 10 cr"}, None)
    assert "never claim personal experience" in seen[0] and "never claim personal experience" not in seen[1]


def test_filing_extraction_falls_back_to_the_light_model(tmp_path, monkeypatch):
    from linkedin_growth_mcp.ipo import rhp

    svc = _svc(tmp_path, gemini_extraction_api_key="ext", gemini_extraction_model="big", gemini_extraction_fallback_model="lite")
    seen = []

    def fake(client, model, pdf, pages):
        seen.append(model)
        if model == "big":
            raise errors.ClientError(429, {"error": {"message": "quota"}})
        return {"ok": True}

    monkeypatch.setattr(rhp, "extract_facts", fake)
    assert svc._extract(object(), b"pdf", [1]) == {"ok": True} and seen == ["big", "lite"]
    assert svc.store.usage_today(svc.cfg.usage_kind("extraction")) == 1  # the extra request is counted
    assert any(r["action"] == "model_fallback" for r in svc.store.conn.execute("SELECT action FROM autopilot_log"))


# --- per-model limits: a model with a small daily or per-minute limit is never hammered ---
def test_rpm_guard_follows_each_models_own_limit():
    from linkedin_growth_mcp.gemini.keys import rpm_guard

    assert rpm_guard("gemini-3.6-flash") == 4       # limit 5 a minute
    assert rpm_guard("gemini-3.1-flash-lite") == 12  # limit 15 a minute
    assert rpm_guard("some-unknown-model") == 12


def test_a_model_at_its_daily_limit_is_skipped_without_wasting_a_request(tmp_path, monkeypatch):
    from linkedin_growth_mcp.gemini import keys

    monkeypatch.setitem(keys.MODEL_LIMITS, "big", {"rpm": 5, "tpm": 250_000, "rpd": 3})  # this system may send 2, one is left spare
    svc = _svc(tmp_path, gemini_api_key="content", gemini_draft_model="big", gemini_draft_fallback_model="lite", daily_gemini_call_cap=10)
    t, log = svc.text, []
    t.client = _ModelFake(log)
    for _ in range(4):
        t._json("hi")
    assert log == ["big", "big", "lite", "lite"]  # from the third request on, straight to the fallback: no refused call to "big"
    assert svc.store.usage_today(keys.model_day_kind(svc.cfg.usage_kind("content"), "big")) == 2


def test_extraction_skips_a_model_at_its_daily_limit(tmp_path, monkeypatch):
    from linkedin_growth_mcp.gemini import keys
    from linkedin_growth_mcp.ipo import rhp

    monkeypatch.setitem(keys.MODEL_LIMITS, "big", {"rpm": 5, "tpm": 250_000, "rpd": 2})
    svc = _svc(tmp_path, gemini_extraction_api_key="ext", gemini_extraction_model="big", gemini_extraction_fallback_model="lite")
    seen = []
    monkeypatch.setattr(rhp, "extract_facts", lambda client, model, pdf, pages: seen.append(model) or {"ok": True})
    for _ in range(3):
        svc._extract(object(), b"pdf", [1])
    assert seen == ["big", "lite", "lite"]  # 'big' may take 1 (limit 2, one spare); later filings go to the fallback
