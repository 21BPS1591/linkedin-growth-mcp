"""Hands-off format and posting-time testing (RULEBOOK.md section E).

One concurrent randomised trial instead of "before vs after": every post day is assigned a (format, slot) cell from a
balanced rotation, so both arms run side by side through the same weeks, news days and follower growth. Format and slot are
crossed and balanced, so they can be tested at the same time without confusing each other.

Noise controls: weekdays only and no listed holidays, every post read at the same age (72h checkpoint), log impressions
(one viral post cannot dominate), values adjusted for post kind (IPO vs other), and a stratified permutation test so
a change must be clearly better, not just lucky. The status quo wins every tie.

Pure functions plus a JSON-serialisable state dict. Nothing here touches the network or the database.
"""
from __future__ import annotations

import itertools
import math
import random
from datetime import date
from statistics import mean
from typing import Any

START_AFTER = date(2026, 11, 4)  # owner's decision: hold the current format until this date
FORMATS = ("long", "short")      # "long" is today's format (the status quo)
SLOTS = (9, 13, 18)              # IST hours, see the research note below
INCUMBENT_SLOT = 9
FORMAT_MIN, FORMAT_MAX = 5, 10   # posts per format arm: first look, final call
SLOT_MIN, SLOT_MAX = 6, 8        # posts per slot: first look, final call
P_FORMAT = 0.10                  # permutation p-value needed to keep the short format
P_SLOT = 0.05                    # stricter for slots: the best of three is being picked
MIN_LIFT = 0.15                  # and at least +15% on the adjusted geometric mean
EARLY_REVERT_LIFT = -0.20        # clearly worse: stop early
# Days with little LinkedIn activity in India (NSE closures; secondary sources, edit with kv "skip_dates" if wrong).
HOLIDAYS = (date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25))

# Slot research: the audience is Indian, senior and at large firms (Bengaluru 22%, Mumbai 14%, Delhi 11%). India-specific
# studies point to weekday mornings around 8 to 11 IST, a lunch peak near 13:00 and an evening B2B slot near 18:00 to 20:00.
# US studies (afternoons in US time) are not used. The studies disagree, so this account's own data decides.
CATALOG: dict[str, str] = {
    "format": "Short (900 to 1,300 character, one idea) posts against the current long format, run side by side.",
    "slot": "Daily slot 9:00, 13:00 and 18:00 IST, run side by side; one is pinned only if clearly better, else 9:00.",
    "image_on": "Not tested: the owner keeps images off.",
}


def new_state() -> dict[str, Any]:
    return {"history": [], "trial": None}


def can_start(today: date) -> bool:
    return today >= START_AFTER


def is_post_day(day: date, extra_skip: Any = ()) -> bool:
    """Weekdays only, and not a listed low-activity holiday. Weekend and holiday posts are noise in any comparison."""
    return day.weekday() < 5 and day not in HOLIDAYS and day.isoformat() not in set(extra_skip or ())


def start_trial(state: dict[str, Any], today: date, first_post_id: int) -> dict[str, Any]:
    if not can_start(today):
        raise ValueError(f"the format is locked until {START_AFTER.isoformat()} (your decision). Nothing was started.")
    if state.get("trial"):
        raise ValueError("a trial is already running. One at a time.")
    state["trial"] = {"started": today.isoformat(), "first_post_id": first_post_id, "days": {}, "decided": {}}
    return state


def cell_for_day(trial: dict[str, Any], day_iso: str) -> dict[str, Any]:
    """The (format, slot) for a post day: the least-used cell so far, ties broken by a per-day seeded shuffle.

    A factor that has already been decided is held at its decided value, so only the open factors keep varying."""
    days, decided = trial["days"], trial["decided"]
    if day_iso in days:
        return days[day_iso]
    values = {"format": FORMATS, "slot": SLOTS}
    open_f = [f for f in ("format", "slot") if f not in decided]
    cells = list(itertools.product(*(values[f] for f in open_f))) if open_f else [()]
    counts = {c: 0 for c in cells}
    for d in days.values():
        key = tuple(d[f] for f in open_f)
        if key in counts:
            counts[key] += 1
    rng = random.Random(day_iso)
    best = min(cells, key=lambda c: (counts[c], rng.random()))
    cell = {f: decided[f]["value"] for f in decided}
    cell.update(dict(zip(open_f, best)))
    days[day_iso] = cell
    return cell


def _perm_p(adj: list[float], labels: list[bool], strata: list[str], iters: int = 4000) -> float:
    """One-sided p that the labelled group's mean is at least this far above the rest, shuffling labels within each stratum."""
    by: dict[str, list[int]] = {}
    for i, s in enumerate(strata):
        by.setdefault(s, []).append(i)
    ks = {s: sum(labels[i] for i in idx) for s, idx in by.items()}
    n_t = sum(ks.values())
    n_f = len(adj) - n_t
    total = sum(adj)
    obs = sum(a for a, l in zip(adj, labels) if l) / n_t - sum(a for a, l in zip(adj, labels) if not l) / n_f
    rng = random.Random(11)
    hits = 0
    for _ in range(iters):
        s_t = sum(adj[i] for s, idx in by.items() for i in rng.sample(idx, ks[s]))
        if s_t / n_t - (total - s_t) / n_f >= obs - 1e-12:
            hits += 1
    return (hits + 1) / (iters + 1)


def _adjusted(rows: list[dict[str, Any]]) -> list[float]:
    """log impressions minus the mean of that post kind: IPO and other posts are compared with their own kind."""
    y = [math.log1p(r["impressions"]) for r in rows]
    kind_mean: dict[str, float] = {}
    for k in {r["kind"] for r in rows}:
        kind_mean[k] = mean(v for v, r in zip(y, rows) if r["kind"] == k)
    return [v - kind_mean[r["kind"]] for v, r in zip(y, rows)]


def _eval_format(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = {f: sum(1 for r in rows if r["format"] == f) for f in FORMATS}
    raw = {f: round(mean(r["impressions"] for r in rows if r["format"] == f)) for f in FORMATS if n[f]}
    out: dict[str, Any] = {"posts": n, "avg_impressions": raw}
    if min(n.values()) < FORMAT_MIN:
        return {**out, "verdict": "running", "note": f"Each format needs {FORMAT_MIN} posts with a 72h reading (have {n})."}
    adj = _adjusted(rows)
    labels = [r["format"] == "short" for r in rows]
    strata = [r["kind"] for r in rows]
    obs = mean(a for a, l in zip(adj, labels) if l) - mean(a for a, l in zip(adj, labels) if not l)
    lift, p = math.exp(obs) - 1, _perm_p(adj, labels, strata)
    out.update(lift_pct=round(100 * lift, 1), p=round(p, 3))
    if p <= P_FORMAT and lift >= MIN_LIFT:
        return {**out, "verdict": "keep", "value": "short", "note": "Short posts are clearly better, not a lucky run."}
    if lift <= EARLY_REVERT_LIFT:
        return {**out, "verdict": "revert", "value": "long", "note": "Short posts are clearly worse: stopped early."}
    if min(n.values()) >= FORMAT_MAX:
        return {**out, "verdict": "revert", "value": "long",
                "note": f"No clear difference after {FORMAT_MAX} posts per format: the old format stays."}
    return {**out, "verdict": "running", "note": f"Not clear yet (lift {100 * lift:+.0f}%, p={p:.2f}); up to {FORMAT_MAX} posts per format."}


def _eval_slot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = {s: sum(1 for r in rows if r["slot"] == s) for s in SLOTS}
    raw = {s: round(mean(r["impressions"] for r in rows if r["slot"] == s)) for s in SLOTS if n[s]}
    out: dict[str, Any] = {"posts": n, "avg_impressions": raw}
    if min(n.values()) < SLOT_MIN:
        return {**out, "verdict": "running", "note": f"Each slot needs {SLOT_MIN} posts with a 72h reading (have {n})."}
    adj = _adjusted(rows)
    strata = [r["kind"] for r in rows]
    obs_by = {}
    for sl in SLOTS:
        lab = [r["slot"] == sl for r in rows]
        obs_by[sl] = mean(a for a, l in zip(adj, lab) if l) - mean(a for a, l in zip(adj, lab) if not l)
    best = max(SLOTS, key=lambda sl: obs_by[sl])  # the slot furthest above the rest of the posts
    best_lift = math.exp(obs_by[best]) - 1
    best_p = _perm_p(adj, [r["slot"] == best for r in rows], strata)
    out.update(best=best, lift_pct=round(100 * best_lift, 1), p=round(best_p, 3))
    if best_p <= P_SLOT and best_lift >= MIN_LIFT:
        return {**out, "verdict": "keep", "value": best, "note": f"{best}:00 is clearly better than the other slots."}
    if min(n.values()) >= SLOT_MAX:
        return {**out, "verdict": "revert", "value": INCUMBENT_SLOT,
                "note": f"No slot is clearly better after {SLOT_MAX} posts each: {INCUMBENT_SLOT}:00 stays."}
    return {**out, "verdict": "running", "note": f"Not clear yet (best {best}:00, lift {100 * best_lift:+.0f}%, p={best_p:.2f})."}


def analyze(trial: dict[str, Any], posts: list[dict[str, Any]]) -> dict[str, Any]:
    """Verdict per factor. posts: {day (iso), kind, impressions} at the 72h checkpoint; the cell comes from the trial's day map."""
    rows = []
    for p in posts:
        cell = trial["days"].get(p["day"])
        if cell:
            rows.append({**p, "format": cell["format"], "slot": cell["slot"]})
    out = {}
    for factor, fn in (("format", _eval_format), ("slot", _eval_slot)):
        d = trial["decided"].get(factor)
        if d:
            out[factor] = {**d["result"], "decided": True, "value": d["value"]}
        elif rows:
            out[factor] = fn(rows)
        else:
            out[factor] = {"verdict": "running", "note": "No posts with a 72h reading yet."}
    return out


def decide(trial: dict[str, Any], results: dict[str, Any], today: date) -> list[str]:
    """Freeze any factor whose verdict is final. Returns the factors decided now. Frozen values stop varying."""
    new = []
    for factor, res in results.items():
        if factor not in trial["decided"] and res["verdict"] in ("keep", "revert"):
            trial["decided"][factor] = {"value": res["value"], "verdict": res["verdict"], "date": today.isoformat(), "result": res}
            new.append(factor)
    return new


def finish(state: dict[str, Any], today: date) -> bool:
    """Archive the trial once both factors are decided (history is kept, never overwritten)."""
    t = state.get("trial")
    if not t or set(t["decided"]) != {"format", "slot"}:
        return False
    state["history"].append({**t, "finished": today.isoformat()})
    state["trial"] = None
    return True
