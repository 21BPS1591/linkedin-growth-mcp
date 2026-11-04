"""Which Gemini key does which job. Keys are never shared between jobs: a job without its own key is paused, not rerouted."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

# Limits the owner read in AI Studio (2026-10-31) for the text model. They apply per Google project, and each key here
# belongs to a different account. The daily caps in .env sit well below RPD so a runaway loop stops long before the key does.
MODEL_LIMITS: dict[str, dict[str, int]] = {
    "gemini-3.1-flash-lite": {"rpm": 15, "tpm": 250_000, "rpd": 500},
    "gemini-3.6-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},  # read in AI Studio 2026-10-31: far tighter than the light model
}
RPM_GUARD = 12  # calls per minute per key: under the 15 RPM limit, so a burst waits instead of being refused

def rpm_guard(model: str) -> int:
    """Calls per minute to allow on a model: one under its limit when known (3.6-flash: 4), never above RPM_GUARD."""
    lim = MODEL_LIMITS.get(model)
    return RPM_GUARD if not lim else max(1, min(RPM_GUARD, lim["rpm"] - 1))


def model_day_kind(key_kind: str, model: str) -> str:
    return f"{key_kind}:{model}"


def model_at_daily_limit(store: Any, key_kind: str, model: str) -> bool:
    """True when this system has already sent (limit - 1) requests to this model today on this key. Only for models with a known
    small daily limit; one request is left spare because the provider also counts requests made outside this system."""
    lim = MODEL_LIMITS.get(model)
    return bool(lim) and store.usage_today(model_day_kind(key_kind, model)) >= lim["rpd"] - 1


def count_model_use(store: Any, key_kind: str, model: str) -> None:
    store.bump_usage(model_day_kind(key_kind, model), 10**9)  # a plain per-model tally, not a cap


_calls: dict[str, deque] = {}
_lock = threading.Lock()


def throttle(kind: str, per_minute: int = RPM_GUARD) -> None:
    """Wait instead of sending more than `per_minute` calls on one key within a minute (never spam a key)."""
    while True:
        with _lock:
            q = _calls.setdefault(kind, deque())
            now = time.monotonic()
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) < per_minute:
                q.append(now)
                return
            wait = 60 - (now - q[0]) + 0.1
        time.sleep(max(wait, 0.1))


PURPOSES: dict[str, dict[str, str]] = {
    "content": {"env": "GEMINI_API_KEY", "label": "Post and content generation",
                "does": "drafting posts, the editor check on a draft, hook suggestions"},
    "image": {"env": "GEMINI_IMAGE_API_KEY", "label": "Image generation", "does": "generating images (generate_image) and nothing else"},
    "extraction": {"env": "GEMINI_EXTRACTION_API_KEY", "label": "IPO filing extraction",
                   "does": "reading IPO filings (RHP PDFs) into verified facts and looking up price bands"},
    "insights": {"env": "GEMINI_INSIGHTS_API_KEY", "label": "Comment and profile analysis",
                 "does": "turning comments into writing rules, scoring conversations, profile-fit and performance summaries"},
}


class KeyMissing(RuntimeError):
    """The job has no key of its own. It is skipped; no other job's key is ever used instead."""

    def __init__(self, purpose: str):
        self.purpose = purpose
        info = PURPOSES[purpose]
        super().__init__(f"{info['env']} is not set: {info['label'].lower()} is paused until a key for it is added to .env")


def describe(cfg: Any, store: Any = None) -> list[dict[str, Any]]:
    """Non-secret overview of every key: owner account, whether it is set, today's use against its cap. Never includes a key."""
    out = []
    for purpose, info in PURPOSES.items():
        row = {"purpose": purpose, "does": info["does"], "env_var": info["env"],
               "account": cfg.account_for(purpose) or "(account not recorded)",
               "configured": bool(cfg.key_for(purpose)), "daily_cap": cfg.cap_for(purpose), "enabled": cfg.cap_for(purpose) > 0,
               "usage_kind": cfg.usage_kind(purpose)}
        if store is not None:
            row["used_today"] = store.usage_today(row["usage_kind"])
        out.append(row)
    if cfg.content_backup_key():
        cap = cfg.daily_content_backup_cap
        row = {"purpose": "content_backup", "does": "backs up CONTENT generation only: used when the content key's daily cap is reached "
               "or Google refuses it", "env_var": PURPOSES[cfg.gemini_content_backup]["env"],
               "account": cfg.content_backup_account() or "(account not recorded)", "configured": True, "daily_cap": cap,
               "enabled": cap > 0, "usage_kind": cfg.content_backup_kind()}
        if store is not None:
            row["used_today"] = store.usage_today(row["usage_kind"])
        out.append(row)
    return out
