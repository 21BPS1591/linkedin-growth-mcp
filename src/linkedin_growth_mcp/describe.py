"""Plain-language description of what the autopilot does, derived from the code that does it.

Nothing here is typed by hand: every line is computed from the constants and config the autopilot actually runs on
(topics.TARGET, autopilot.PILLAR_ARMS, gates.*, gemini.text.*, experiments.*, Config). Change those and this
description, and the dashboard that renders it, follow on their own.
"""
from __future__ import annotations

from .config import Config
from .strategy import experiments, gates, topics
from .strategy.styles import HOOKS, STYLES


def topic_group(series: str | None) -> str:
    """The topic group a backlog series belongs to (posts drafted without an idea count as stories)."""
    return topics.GROUP_OF.get(series or "story", series or "story")


def scope(cfg: Config) -> str:
    """One line: what kinds of post may go out this week."""
    mix = ", ".join(f"{v:g} {k}" for k, v in topics.TARGET.items() if v > 0 and k != "ipo")
    if cfg.ipo_posts:
        ipo = f"up to {cfg.ipo_max_per_week} IPO posts (quality {cfg.ipo_min_quality:g}+)"
        if topics.TARGET.get("ipo", 0) > 0:
            ipo += f", target {topics.TARGET['ipo']:g}"
        return f"{ipo} plus {mix} per week"
    return f"domain only: {mix} per week; IPO posts and the weekend scorecard are OFF (IPO_POSTS=false)"


def posting_rules(cfg: Config) -> list[tuple[str, str]]:
    """(label, detail) rows for the rules every domain post passes through, in the order they apply."""
    from . import autopilot  # imported here: autopilot pulls in the whole pipeline

    from .gemini import text as draft_text

    styles = sorted({a.split("|")[0] for a in autopilot.PILLAR_ARMS})
    hooks = sorted({a.split("|")[1] for a in autopilot.PILLAR_ARMS})
    not_drawn = sorted(set(STYLES) - set(styles))
    unused_hooks = sorted(set(HOOKS) - set(hooks))
    lo, hi = gates.GENERAL_LENGTH
    rows = [
        ("Weekly mix", ", ".join(f"{v:g} {k}" for k, v in topics.TARGET.items() if v > 0 and k != "ipo")
         + f" (weekdays only; every group retried at least every {topics.FLOOR_DAYS} days)"),
        ("Styles drawn", ", ".join(styles) + (f"; not drawn: {', '.join(not_drawn)}" if not_drawn else "")),
        ("Openers drawn", ", ".join(hooks) + (f"; not drawn: {', '.join(unused_hooks)}" if unused_hooks else "")),
        ("Opener rule (prompt)", draft_text.OPENER_RULE),
        ("Line rule (prompt)", draft_text.LINE_RULE),
        ("Closer rule (prompt)", draft_text.CLOSER_RULE),
        ("Soft lint", ("a line over %d words or a last line" % gates.MAX_LINE_WORDS if gates.LINE_RULE_ENABLED else "a last line") + f" that is not a question earns one rewrite with hints; "
                      "a second miss is accepted, so these never cost the day's post"),
        ("Comment automation", (f"on: one comment an hour on strong niche posts (25+ reactions, under 4 days old), at most {cfg.engage_daily_cap} a day, "
                                f"the first when the laptop opens, one per author a week, {cfg.engage_hours[0]}:00 to {cfg.engage_hours[1]}:00"
                                if cfg.engage else "off")),
        ("Opener gate", f"a draft whose first {gates.OPENER_WORDS} words match any of the last {gates.RECENT_OPENERS} posts is redrafted"),
        ("Form gates", f"{lo} to {hi} characters, at most {gates.MAX_HASHTAGS} hashtags, no links or mentions, no em or en dashes, "
                       "no advice wording, every number sourced from the story bank"),
        ("Attempts", f"up to {autopilot.MAX_BLOCKED_ATTEMPTS} drafts per day; editor score {cfg.editor_min_score:g}+ "
                     + ("required (one rewrite, then best of the two)" if cfg.editor_gate else "not checked")),
        ("Posting time", (f"decided by the running {' / '.join(f'{h}:00' for h in experiments.SLOTS)} test"
                          if cfg.auto_experiments else f"{cfg.post_hour}:00")
         + f"; formats under test: {', '.join(experiments.FORMATS)}"),
        ("Weekend scorecard", "on" if (cfg.ipo_posts and autopilot.WEEKLY_SCORECARD_ENABLED) else "off"),
    ]
    return rows


def jobs(cfg: Config, post_hour: int) -> list[tuple[str, str]]:
    """What runs when, for the dashboard's schedule card."""
    from . import autopilot

    return [
        ("Autopilot cycle", "every 60 min"),
        ("Daily post slot", f"{post_hour}:00 (draft opens {autopilot.DRAFT_LEAD_MINUTES} min before)"),
        ("Weekend IPO scorecard", "Sat/Sun after the slot" if (cfg.ipo_posts and autopilot.WEEKLY_SCORECARD_ENABLED) else "off"),
        ("Market data sync", "hourly" if cfg.market_sync else "off"),
        ("Metrics refresh", "every 6 h"),
    ]
