"""Local health dashboard for the LinkedIn Growth MCP. Read-only, with ONE exception: the idea Veto/Restore buttons
change an idea's status (backlog <-> dropped) in growth.db. Nothing is ever deleted and the server is never touched.

Every rule, mix, style list and schedule on the page is read from the package (linkedin_growth_mcp.describe), never typed
here, so a change to the autopilot code shows up on the page by itself. Do not add rule text to this file.

Run: dashboard.cmd  ->  http://127.0.0.1:8790  (auto-refreshes every 60 s)
Stdlib only, so it runs with the project venv or any Python 3.11+.
"""
from __future__ import annotations

import html
import json
import secrets
import socket
import sqlite3
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8790
TOKEN = secrets.token_hex(16)  # changes every time the dashboard starts; the page embeds it in each veto form
SERVER_PORT = 8000
sys.path.insert(0, str(ROOT / "src"))
from linkedin_growth_mcp import describe  # noqa: E402  (the page describes the code; nothing is retyped here)
from linkedin_growth_mcp.config import load_config  # noqa: E402


def db() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{(ROOT / 'data' / 'growth.db').as_posix()}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def kv(c: sqlite3.Connection, key: str):
    r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r[0])
    except ValueError:
        return r[0]


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = max(0, time.time() - ts)
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{int(s // 60)} min ago"
    if s < 172800:
        return f"{s / 3600:.1f} h ago"
    return f"{int(s // 86400)} d ago"


def stamp(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M") if ts else "-"


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def collect() -> dict:
    cfg = load_config()  # secrets stay in memory and are never rendered: only named, non-secret fields are read below
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    checks: list[tuple[str, str, str]] = []  # (state ok|warn|bad, name, detail)

    def add(state: str, name: str, detail: str) -> None:
        checks.append((state, name, detail))

    c = db()
    try:
        add("ok" if port_open(SERVER_PORT) else "bad", "MCP server",
            f"listening on :{SERVER_PORT}" if port_open(SERVER_PORT) else f"NOT listening on :{SERVER_PORT}. Run start-server.cmd")

        last = c.execute("SELECT MAX(ts) FROM autopilot_log").fetchone()[0]
        age = (now - last) / 60 if last else 1e9
        add("ok" if age < 90 else "warn" if age < 180 else "bad", "Hourly autopilot cycle",
            f"last activity {ago(last)} ({stamp(last)})")

        mode = kv(c, "autonomy")
        add("ok" if mode == "live" else "warn", "Autonomy mode", str(mode))

        ipo_on = cfg.ipo_posts
        add("ok", "Posting scope", describe.scope(cfg))

        exps = kv(c, "experiments") or {}
        trial, fmt, slot = exps.get("trial"), kv(c, "format_mode") or "long", kv(c, "post_hours")
        if now < datetime(2026, 11, 4).timestamp():
            add("ok", "Format + slot trial", "locked until 4 Nov (your decision); the format is unchanged")
        elif trial:
            dec = trial.get("decided", {})
            add("ok", "Format + slot trial",
                f"running since {trial['started']}, {len(trial.get('days', {}))} weekdays assigned; decided: "
                + (", ".join(f"{k}={v['value']}" for k, v in dec.items()) or "nothing yet"))
        else:
            add("ok", "Format + slot trial", f"finished; format {fmt}, slot " + (f"{slot[0]}:00" if slot else "9:00"))

        sess = kv(c, "browser_session") or {}
        add("ok" if sess.get("ok") else "bad", "LinkedIn browser login",
            f"{'working' if sess.get('ok') else 'EXPIRED, run --login'}, checked {ago(sess.get('checked_at'))}")

        try:
            tok = json.loads((ROOT / "data" / "linkedin_token.json").read_text(encoding="utf-8"))
            left = (tok["obtained_at"] + tok["expires_in"] - now) / 86400
            add("ok" if left > 14 else "warn" if left > 3 else "bad", "LinkedIn posting token", f"expires in {left:.0f} days")
        except (OSError, KeyError, ValueError):
            add("bad", "LinkedIn posting token", "token file missing or unreadable")

        posted_today = c.execute("SELECT COUNT(*) FROM posts WHERE date(published_at,'unixepoch','localtime')=?", (today,)).fetchone()[0]
        hour = datetime.now().hour
        post_hour = cfg.post_hour
        # The autopilot posts on weekdays only (minus listed holidays / kv skip_dates), so a day off is not a problem.
        post_day = datetime.now().weekday() < 5 and today not in set(kv(c, "skip_dates") or [])
        try:
            sys.path.insert(0, str(ROOT / "src"))
            from linkedin_growth_mcp.strategy.experiments import is_post_day

            post_day = is_post_day(datetime.now().date(), kv(c, "skip_dates") or [])
        except Exception:
            pass  # keep the weekday-only answer
        trial = (kv(c, "experiments") or {}).get("trial") or {}
        cell = (trial.get("days") or {}).get(today)
        slot_note = ""
        if cell:  # the running time test gives each day its own slot; that, not POST_HOUR, is when today's post is due
            post_hour = int(cell["slot"])
            slot_note = f" (time test: {cell['format']} format at {post_hour}:00)"
        due = [i for i in (kv(c, "scheduled_publishes") or []) if datetime.fromtimestamp(i["at"]).date().isoformat() == today]
        if posted_today:
            add("ok", "Posted today", f"{posted_today} post(s) published")
        elif due:
            when = datetime.fromtimestamp(min(i["at"] for i in due)).strftime("%H:%M")
            add("ok" if time.time() <= min(i["at"] for i in due) + 900 else "bad", "Posted today",
                f"nothing yet; draft {due[0]['draft_id']} is scheduled for {when}")
        elif not post_day:
            add("ok", "Posted today", "no post planned today (weekend or holiday)")
        else:
            add("ok" if hour < post_hour else "warn" if hour <= post_hour + 2 else "bad", "Posted today",
                f"nothing yet (today's slot {post_hour}:00{slot_note})" if hour >= post_hour else f"not due yet (today's slot {post_hour}:00{slot_note})")

        usage = {r["kind"]: r["n"] for r in c.execute("SELECT kind,n FROM usage WHERE day=?", (today,))}
        keys = kv(c, "gemini_keys") or []
        for k in keys:
            used, cap = usage.get(k["usage_kind"], 0), k["daily_cap"]
            if not k.get("enabled", True):
                add("ok", f"Gemini {k['purpose']} key ({k['account']})", "switched off on purpose (paid model, billing off); text work comes first")
            elif not k["configured"]:
                add("warn", f"Gemini {k['purpose']} key", f"no key, so this is paused: {k['does']} (set {k['env_var']})")
            else:
                add("ok" if used < cap - 2 else "warn" if used < cap else "bad", f"Gemini {k['purpose']} key ({k['account']})",
                    f"{used} of {cap} today")
        b, bcap = usage.get("browser_page", 0), cfg.daily_browser_page_cap
        add("ok" if b < bcap * 0.8 else "warn" if b < bcap else "bad", "Browser pages today", f"{b} of {bcap}")

        bk = kv(c, "last_backup")
        add("ok" if bk == today else "warn", "Daily DB backup", f"last backup {bk}")

        snap = kv(c, "last_analytics_snapshot")
        add("ok" if snap and now - snap < 5400 else "warn", "Analytics snapshot (hourly)", ago(snap))

        errs = c.execute("SELECT ts,action,detail FROM autopilot_log WHERE ts>? AND (action LIKE '%error%' OR action IN ('blocked','alert')) ORDER BY ts DESC",
                         (now - 86400,)).fetchall()
        add("ok" if not errs else "warn", "Errors / alerts, last 24 h", f"{len(errs)} event(s)" if errs else "none")

        rank = {"bad": 2, "warn": 1, "ok": 0}
        worst = max((rank[s] for s, _, _ in checks), default=0)
        overall = ["ok", "warn", "bad"][worst]

        posts = c.execute("""
            SELECT p.id, p.url, p.published_at, d.style, d.hook, i.series,
                   (SELECT impressions FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) imp,
                   (SELECT reactions FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) rea,
                   (SELECT comments FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) com,
                   (SELECT reposts FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) rep,
                   (SELECT captured_at FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) cap,
                   (SELECT num FROM post_audience a WHERE a.post_id=p.id AND a.value='Out-of-network share'
                    ORDER BY captured_at DESC LIMIT 1) oon
            FROM posts p JOIN drafts d ON d.id=p.draft_id LEFT JOIN ideas i ON i.draft_id=p.draft_id
            ORDER BY p.published_at DESC""").fetchall()
        gone = set(kv(c, "ignored_posts") or [])  # deleted on LinkedIn: hidden here, rows stay in the DB
        posts = [p for p in posts if p["id"] not in gone]
        # What works: domain posts only (IPO posts are a different audience), averaged by style and by topic group
        works: dict[str, dict[str, list]] = {"style": {}, "topic": {}}
        for p in posts:
            if p["imp"] is None or p["series"] == "ipo":
                continue
            works["style"].setdefault(p["style"], []).append(p)
            works["topic"].setdefault(describe.topic_group(p["series"]), []).append(p)
        weeks: dict[str, dict] = {}  # growth proof: per ISO week, posts and their latest metrics (oldest week = baseline)
        for p in reversed(posts):
            y, w, _ = datetime.fromtimestamp(p["published_at"]).isocalendar()
            k = weeks.setdefault(f"{y}-W{w:02d}", dict(posts=0, imp=0, com=0, rea=0))
            k["posts"] += 1
            k["imp"] += p["imp"] or 0
            k["com"] += p["com"] or 0
            k["rea"] += p["rea"] or 0
        all_fol = c.execute("SELECT day,count FROM followers ORDER BY day").fetchall()
        followers = c.execute("SELECT day,count FROM followers ORDER BY day DESC LIMIT 7").fetchall()
        log = c.execute("SELECT ts,action,detail FROM autopilot_log ORDER BY id DESC LIMIT 25").fetchall()
        ipos = c.execute("SELECT company, verified, data FROM ipos ORDER BY id DESC").fetchall()
        counts = {
            "Drafts": c.execute("SELECT COUNT(*) FROM drafts").fetchone()[0],
            "Published": len(posts),
            "Metric readings kept": c.execute("SELECT COUNT(*) FROM metrics_log").fetchone()[0],
            "Audience rows kept": c.execute("SELECT COUNT(*) FROM post_audience").fetchone()[0],
            "IPO facts kept": c.execute("SELECT COUNT(*) FROM cg_facts").fetchone()[0],
            "Ideas in backlog": c.execute("SELECT COUNT(*) FROM ideas WHERE status='backlog'").fetchone()[0],
        }
        ideas = c.execute(
            "SELECT id, series, title, priority, status FROM ideas WHERE status IN ('backlog','dropped') "
            + ("" if ipo_on else "AND series!='ipo' ")
            + "ORDER BY status='dropped', priority, id LIMIT 60").fetchall()
        jobs = describe.jobs(cfg, post_hour)
        rules = describe.posting_rules(cfg)
        dbsize = (ROOT / "data" / "growth.db").stat().st_size / 1e6
    finally:
        c.close()
    return dict(keys=keys, weeks=weeks, all_fol=all_fol, checks=checks, overall=overall, posts=posts, followers=followers, log=log, ipos=ipos, counts=counts,
                jobs=jobs, errs=errs, dbsize=dbsize, now=now, ideas=ideas, ipo_on=ipo_on, rules=rules, works=works)


def set_idea_status(idea_id: int, action: str) -> bool:
    """The dashboard's only write: veto (backlog -> dropped) or restore (dropped -> backlog). Nothing is ever deleted."""
    new, old = ("dropped", "backlog") if action == "veto" else ("backlog", "dropped")
    c = sqlite3.connect((ROOT / "data" / "growth.db").as_posix(), timeout=10)
    try:
        cur = c.execute("UPDATE ideas SET status=? WHERE id=? AND status=?", (new, idea_id, old))
        c.commit()
        return cur.rowcount == 1
    finally:
        c.close()


def esc(x) -> str:
    return html.escape(str(x))


def pct(x) -> str:
    return f"{x:.0f}%" if x is not None else "-"


def _k(n: float) -> str:
    """Compact figure for tiles: 1,284 or 12.9K."""
    return f"{n / 1000:.1f}K" if abs(n) >= 10000 else f"{n:,.0f}"


def _delta(cur: float, prev: float | None, suffix: str = "") -> str:
    """Signed change against a previous period; green when up (up is good for every tile here)."""
    if prev is None:
        return '<span class="delta mut">no earlier period yet</span>'
    diff = cur - prev
    cls = "up" if diff > 0 else "down" if diff < 0 else "mut"
    arrow = "&#9650;" if diff > 0 else "&#9660;" if diff < 0 else "&#8212;"
    return f'<span class="delta {cls}">{arrow} {diff:+,.0f}{suffix} vs previous</span>'


def _sparkline(values: list[float], w: int = 120, h: int = 32) -> str:
    """12-point line in the de-emphasis hue, the latest point in the accent."""
    vals = [float(v) for v in values][-12:]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pts = [(2 + i * (w - 4) / (len(vals) - 1), 2 + (h - 4) * (1 - (v - lo) / span)) for i, v in enumerate(vals)]
    d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<polyline points="{d}" fill="none" stroke="var(--accsoft)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--acc)" stroke="var(--card)" stroke-width="2"/></svg>')


def _bars(posts: list) -> str:
    """Impressions per published post, oldest first. One measure, two identities (domain / IPO) carried by hue and legend."""
    rows = [p for p in reversed(posts) if p["imp"] is not None]
    if not rows:
        return '<p class="mut">no measured posts yet</p>'
    W, H, L, B, T = 820, 220, 44, 34, 12
    n, mx = len(rows), max(p["imp"] for p in rows) or 1
    step = 100 if mx <= 600 else 250 if mx <= 1500 else 500
    top = ((mx // step) + 1) * step
    slot = (W - L - 8) / n
    bw = max(6.0, min(28.0, slot - 8))
    ih = H - B - T
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Impressions per post">']
    for g in range(0, top + 1, step):  # recessive grid, labels in muted ink
        y = T + ih * (1 - g / top)
        out.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W - 4}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{L - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">{g:,}</text>')
    every = max(1, -(-n // 14))
    for i, p in enumerate(rows):
        x = L + 4 + i * slot + (slot - bw) / 2
        y = T + ih * (1 - p["imp"] / top)
        base = T + ih
        r = min(4.0, bw / 2)
        path = (f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + bw - r:.1f} "
                f"Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f} V{base:.1f} Z")
        kind = "ipo" if p["series"] == "ipo" else "dom"
        when = stamp(p["published_at"])
        tip = (f"{when} · {p['series'] or 'story'} · {p['style']} / {p['hook']} · {p['imp']:,} impressions"
               + (f" · {p['oon']:.0f}% out of network" if p["oon"] is not None else "")
               + f" · {p['rea'] or 0} reactions, {p['com'] or 0} comments")
        out.append(f'<g class="bar {kind}"><title>{esc(tip)}</title>'
                   f'<rect x="{x - 3:.1f}" y="{T}" width="{bw + 6:.1f}" height="{ih}" class="hit"/>'
                   f'<path d="{path}"/>'
                   + (f'<text x="{x + bw / 2:.1f}" y="{base + 16}" class="tick" text-anchor="middle">{esc(when[:6])}</text>'
                      if i % every == 0 else "")
                   + "</g>")
    out.append(f'<line x1="{L}" y1="{T + ih}" x2="{W - 4}" y2="{T + ih}" class="axis"/></svg>')
    return "".join(out)


def render(d: dict) -> str:
    label = {"ok": "All systems working", "warn": "Working, needs a look", "bad": "Problem, action needed"}[d["overall"]]
    checks = "".join(
        f'<div class="chk"><span class="dot {s}"></span><div><b>{esc(n)}</b><span class="mut">{esc(t)}</span></div></div>'
        for s, n, t in d["checks"])
    mx = max((p["imp"] or 0) for p in d["posts"]) or 1
    posts = "".join(
        f'<tr{" class=dim" if p["series"] == "ipo" else ""}><td class="nw">{esc(stamp(p["published_at"]))}</td>'
        f'<td class="nw">{esc(describe.topic_group(p["series"]))}</td><td class="nw">{esc(p["style"])}</td>'
        f'<td class="hook" title="{esc(p["hook"] or "")}">{"<a href=%s target=_blank rel=noopener>" % esc(p["url"]) if p["url"] else ""}'
        f'{esc(p["hook"] or "")}{"</a>" if p["url"] else ""}</td>'
        f'<td class="n nw"><span class="ib"><i style="width:{(p["imp"] or 0) / mx * 100:.0f}%"></i></span>{p["imp"] if p["imp"] is not None else "-"}</td>'
        f'<td class="n nw">{pct(p["oon"])}</td><td class="n nw">{p["rea"] or 0}</td><td class="n nw">{p["com"] or 0}</td>'
        f'<td class="n nw">{p["rep"] or 0}</td><td class="mut nw">{esc(ago(p["cap"]))}</td></tr>'
        for p in d["posts"]) or '<tr><td colspan="10" class="mut">no posts yet</td></tr>'

    # KPI tiles: followers (hero), average reach of the last 5 domain posts against the 5 before, out-of-network share, comments
    fr = d["all_fol"]
    fol_now = fr[-1]["count"] if fr else 0
    fol_prev = fr[0]["count"] if len(fr) >= 2 else None
    dom = [p for p in d["posts"] if p["imp"] is not None and p["series"] != "ipo"]  # newest first
    last5, prev5 = dom[:5], dom[5:10]
    avg = lambda rows, key: (sum((r[key] or 0) for r in rows) / len(rows)) if rows else 0.0
    oon_rows = [p for p in last5 if p["oon"] is not None]
    tiles = [
        ("Followers", f'{fol_now:,}', _delta(fol_now, fol_prev) if fr else '<span class="delta mut">no reading yet</span>',
         _sparkline([r["count"] for r in fr]), True),
        ("Avg impressions, last 5 domain posts", _k(avg(last5, "imp")), _delta(avg(last5, "imp"), avg(prev5, "imp") if prev5 else None),
         _sparkline([p["imp"] for p in reversed(dom[:12])]), False),
        ("Out-of-network share, last 5", f'{avg(oon_rows, "oon"):.0f}%' if oon_rows else "-",
         _delta(avg(oon_rows, "oon"), avg([p for p in prev5 if p["oon"] is not None], "oon") if prev5 else None, " pts") if oon_rows else "", "", False),
        ("Comments, last 5 posts", f'{sum((p["com"] or 0) for p in last5)}', _delta(sum((p["com"] or 0) for p in last5),
         sum((p["com"] or 0) for p in prev5) if prev5 else None), "", False),
        ("Published", f'{len(d["posts"])}', f'<span class="delta mut">{len(dom)} domain, {len(d["posts"]) - len(dom)} IPO</span>', "", False),
    ]
    kpis = "".join(
        f'<div class="tile{" hero" if hero else ""}"><span class="lbl">{esc(lab)}</span><div class="row"><b>{val}</b>{spark}</div>{delta}</div>'
        for lab, val, delta, spark, hero in tiles)

    def works_rows(groups: dict[str, list]) -> str:
        rows_ = sorted(groups.items(), key=lambda kv_: -sum(p["imp"] for p in kv_[1]) / len(kv_[1]))
        top = max((sum(p["imp"] for p in v) / len(v) for v in groups.values()), default=1) or 1
        return "".join(
            f'<tr><td class="nw">{esc(k)}</td><td class="n nw">{len(v)}</td>'
            f'<td class="n nw"><span class="ib"><i style="width:{sum(p["imp"] for p in v) / len(v) / top * 100:.0f}%"></i></span>{sum(p["imp"] for p in v) / len(v):.0f}</td>'
            f'<td class="n nw">{sum((p["rea"] or 0) for p in v) / len(v):.1f}</td></tr>' for k, v in rows_)

    empty = '<tr><td colspan="4" class="mut">no measured domain posts yet</td></tr>'
    works = (f'<p class="mut lead">Domain posts only, latest reading per post. These averages set the posting rules; a group with one or two posts is a hint, not proof.</p>'
             f'<div class="two"><div class="tbl"><table><thead><tr><th>Topic</th><th class="n">Posts</th><th class="n">Avg impr.</th><th class="n">Avg react.</th></tr></thead>'
             f'<tbody>{works_rows(d["works"]["topic"]) or empty}</tbody></table></div>'
             f'<div class="tbl"><table><thead><tr><th>Style</th><th class="n">Posts</th><th class="n">Avg impr.</th><th class="n">Avg react.</th></tr></thead>'
             f'<tbody>{works_rows(d["works"]["style"]) or empty}</tbody></table></div></div>')
    rules_tbl = "".join(f'<tr><td class="nw nm">{esc(a)}</td><td class="mut">{esc(b)}</td></tr>' for a, b in d["rules"])

    def fol_row(r) -> str:  # a reading far from the newest one is a scrape error (an old parser once grabbed other accounts' counts)
        bad = fol_now and r["count"] > fol_now * 20
        return (f'<tr><td class="nw">{esc(r["day"])}</td><td class="n nw"{" style=text-decoration:line-through" if bad else ""}>{r["count"]:,}</td>'
                f'<td class="mut">{"scrape error, ignore" if bad else ""}</td></tr>')

    fol = "".join(fol_row(r) for r in d["followers"]) or '<tr><td colspan="3" class="mut">no reading yet</td></tr>'
    wrows = "".join(
        f'<tr><td class="nw">{esc(k)}</td><td class="n nw">{v["posts"]}</td><td class="n nw">{v["imp"] / v["posts"]:.0f}</td>'
        f'<td class="n nw">{v["rea"] / v["posts"]:.1f}</td><td class="n nw">{v["com"] / v["posts"]:.1f}</td></tr>' for k, v in d["weeks"].items())
    proof = (f'<p class="mut lead">The newest week is still maturing (posts keep collecting impressions for days), and a few posts a week cannot prove a trend. Judge after 8 to 12 weeks.</p>'
             f'<div class="tbl"><table><thead><tr><th>Week</th><th class="n">Posts</th><th class="n">Avg impressions</th><th class="n">Avg reactions</th><th class="n">Avg comments</th></tr></thead><tbody>{wrows}</tbody></table></div>')
    log = "".join(
        f'<tr class="{"errrow" if "error" in r["action"] or r["action"] in ("blocked", "alert") else ""}">'
        f'<td class="mut nw">{esc(stamp(r["ts"]))}</td><td class="nw">{esc(r["action"])}</td><td class="mut">{esc(r["detail"][:160])}</td></tr>'
        for r in d["log"]) or '<tr><td colspan="3" class="mut">nothing logged yet</td></tr>'
    ipo = "".join(
        f'<tr><td>{esc(r["company"].title())}</td><td class="nw">{"verified" if r["verified"] else "<span class=mut>unverified</span>"}</td></tr>'
        for r in d["ipos"])

    def idea_row(r) -> str:
        veto = r["status"] == "backlog"
        btn = (f'<form method="post" action="/idea"><input type="hidden" name="token" value="{TOKEN}">'
               f'<input type="hidden" name="id" value="{r["id"]}"><input type="hidden" name="action" value="{"veto" if veto else "restore"}">'
               f'<button class="vbtn{"" if veto else " rest"}">{"Veto" if veto else "Restore"}</button></form>')
        return (f'<tr class="{"" if veto else "dim"}"><td class="nw">{esc(r["series"])}</td><td>{esc(r["title"])}</td>'
                f'<td class="mut nw">{"queued" if veto else "vetoed"}</td><td class="nw">{btn}</td></tr>')

    ideas = "".join(idea_row(r) for r in d["ideas"]) or '<tr><td colspan="4" class="mut">no ideas in the backlog</td></tr>'
    stats = "".join(f'<div class="stat"><b>{v:,}</b><span>{esc(k)}</span></div>' for k, v in d["counts"].items())
    jobs = "".join(f'<tr><td class="nw nm">{esc(a)}</td><td class="mut">{esc(b)}</td></tr>' for a, b in d["jobs"])
    keyrows = "".join(
        f'<tr><td class="nw">{esc(k["purpose"])}</td><td class="nw">{esc(k["account"])}</td>'
        f'<td class="nw">{"off on purpose" if not k.get("enabled", True) else "set" if k["configured"] else "<b class=bad>missing</b>"}</td>'
        f'<td class="mut">{esc(k["does"])}</td></tr>' for k in d["keys"]) or "<tr><td colspan=4 class=mut>restart the server once to publish this</td></tr>"
    ipo_card = f'<section class="card"><h2>Tracked IPOs</h2><div class="tbl"><table><tbody>{ipo}</tbody></table></div></section>' if d["ipo_on"] else ""
    legend = ('<div class="legend"><span><i class="sw dom"></i>domain post</span><span><i class="sw ipo"></i>IPO post (before the switch-off)</span>'
              '<span class="mut">hover a bar for the detail</span></div>')
    updated = esc(datetime.fromtimestamp(d["now"]).strftime("%d %b %Y, %H:%M:%S"))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>LinkedIn Growth</title><style>
:root{{color-scheme:dark;--bg:#121211;--card:#1a1a19;--card2:#222220;--line:#2d2d2a;--tx:#f0efec;--tx2:#c3c2b7;--mut:#8f8e86;
--acc:#3987e5;--accsoft:#1c5cab;--ok:#0ca30c;--warn:#fab219;--bad:#d03b3b}}
*{{box-sizing:border-box}}html{{background:var(--bg)}}
body{{margin:0;font:14px/1.5 system-ui,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--tx);-webkit-font-smoothing:antialiased}}
main{{max-width:1280px;margin:0 auto;padding:20px 16px 48px}}
a{{color:inherit}}b{{font-weight:600}}
.banner{{display:flex;align-items:center;gap:14px;padding:16px 20px;border-radius:14px;background:var(--card);border:1px solid var(--line);border-left:6px solid var(--{d["overall"]})}}
.banner h1{{margin:0;font-size:20px;font-weight:600;line-height:1.3}}.banner small{{display:block;color:var(--mut);margin-top:2px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;min-width:0;display:flex;flex-direction:column;gap:6px}}
.tile .lbl{{color:var(--mut);font-size:12px;line-height:1.3}}.tile .row{{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;flex-wrap:wrap}}
.tile b{{font-size:28px;line-height:1.1;font-weight:600;letter-spacing:-.01em}}.tile.hero b{{font-size:48px}}
.tile .spark{{flex:0 0 auto}}.delta{{font-size:12px;line-height:1.3}}.delta.up{{color:var(--ok)}}.delta.down{{color:var(--bad)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px;margin-top:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;min-width:0}}.wide{{grid-column:1/-1}}
h2{{margin:0 0 10px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}}
.lead{{margin:0 0 10px}}.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}}
.tbl{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
table{{width:100%;border-collapse:collapse}}thead th{{position:sticky;top:0;background:var(--card)}}
td,th{{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;overflow-wrap:anywhere}}
th{{color:var(--mut);font-weight:600;font-size:12px;white-space:nowrap}}tbody tr:last-child td{{border-bottom:0}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}.nw{{white-space:nowrap;overflow-wrap:normal}}.nm{{font-weight:500;overflow-wrap:normal}}
.chks{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}}
.chk{{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--line);border-radius:10px;min-width:0;background:var(--card2)}}
.chk .dot{{flex:0 0 auto;margin-top:5px}}.chk>div{{min-width:0}}.chk b{{display:block;overflow-wrap:anywhere}}.chk .mut{{display:block;font-size:13px;overflow-wrap:anywhere}}
.mut{{color:var(--mut)}}.bad{{color:var(--bad)}}.dim td{{opacity:.5}}
.hook{{max-width:460px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.hook a{{text-decoration:none;border-bottom:1px dotted var(--mut)}}
.errrow td{{background:color-mix(in srgb,var(--bad) 14%,transparent)}}
.dot{{display:inline-block;width:11px;height:11px;border-radius:50%;vertical-align:middle;margin-top:3px}}.dot.ok{{background:var(--ok)}}.dot.warn{{background:var(--warn)}}.dot.bad{{background:var(--bad)}}
.ib{{display:inline-block;width:64px;height:6px;border-radius:3px;background:var(--card2);vertical-align:middle;margin-right:8px;overflow:hidden}}
.ib i{{display:block;height:100%;border-radius:3px;background:var(--acc)}}
.chart{{width:100%;height:auto;display:block}}.chart .grid{{stroke:var(--line);stroke-width:1}}.chart .axis{{stroke:var(--mut);stroke-width:1}}
.chart .tick{{fill:var(--mut);font-size:11px}}.chart .bar path{{fill:var(--acc)}}.chart .bar.ipo path{{fill:var(--mut)}}
.chart .hit{{fill:transparent}}.chart .bar:hover path{{fill:var(--tx)}}
.legend{{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--tx2);margin:6px 0 2px}}.legend span{{display:inline-flex;align-items:center;gap:6px}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:2px}}.sw.dom{{background:var(--acc)}}.sw.ipo{{background:var(--mut)}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}}.stat{{border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:0}}
.stat b{{display:block;font-size:20px;line-height:1.2}}.stat span{{color:var(--mut);font-size:12px;line-height:1.3;display:block}}
.vbtn{{background:transparent;color:var(--bad);border:1px solid var(--bad);border-radius:6px;padding:3px 10px;cursor:pointer;font:inherit;font-size:12px}}.vbtn.rest{{color:var(--ok);border-color:var(--ok)}}
form{{margin:0}}
@media(max-width:600px){{main{{padding:12px 12px 40px}}.tile.hero b{{font-size:40px}}.hook{{max-width:220px}}.chart .tick{{font-size:20px}}}}
</style></head><body><main>
<div class="banner"><span class="dot {d["overall"]}" style="width:20px;height:20px;margin:0"></span>
<div><h1>{label}</h1><small>Updated {updated} · refreshes every 60 s · database {d["dbsize"]:.1f} MB · read-only view</small></div></div>
<div class="kpis">{kpis}</div>
<div class="grid">
<section class="card wide"><h2>Impressions per post</h2>{legend}{_bars(d["posts"])}</section>
<section class="card wide"><h2>Health checks</h2><div class="chks">{checks}</div></section>
<section class="card"><h2>Scheduled jobs</h2><div class="tbl"><table><tbody>{jobs}</tbody></table></div></section>
<section class="card"><h2>Data kept (never deleted)</h2><div class="stats">{stats}</div></section>
<section class="card wide"><h2>Posts and latest metrics</h2><div class="tbl"><table><thead><tr><th>Published</th><th>Topic</th><th>Style</th><th>Hook</th>
<th class="n">Impressions</th><th class="n">Out of net.</th><th class="n">Reactions</th><th class="n">Comments</th><th class="n">Reposts</th><th>Read</th></tr></thead><tbody>{posts}</tbody></table></div>
{"" if d["ipo_on"] else '<p class="mut" style="margin:10px 0 0">Dimmed rows are the earlier IPO posts; they no longer count toward the style choice.</p>'}</section>
<section class="card wide"><h2>What works so far</h2>{works}</section>
<section class="card wide"><h2>Posting rules (read from the code)</h2><div class="tbl"><table><tbody>{rules_tbl}</tbody></table></div></section>
<section class="card wide"><h2>Growth proof, by week</h2>{proof}</section>
<section class="card"><h2>Followers (last 7 readings)</h2><div class="tbl"><table><tbody>{fol}</tbody></table></div></section>
<section class="card wide"><h2>Gemini keys: which account does which job</h2><div class="tbl"><table><thead><tr><th>Job</th><th>Account</th><th>Key</th><th>Used for</th></tr></thead><tbody>{keyrows}</tbody></table></div></section>
{ipo_card}
<section class="card wide" id="ideas"><h2>Post ideas: veto what you do not want posted</h2>
<p class="mut lead">Queued ideas are posted in the order the topic mix picks them. Veto keeps the idea (nothing is deleted) but the autopilot will never use it; Restore puts it back.</p>
<div class="tbl"><table><thead><tr><th>Series</th><th>Idea</th><th>State</th><th></th></tr></thead><tbody>{ideas}</tbody></table></div></section>
<section class="card wide"><h2>Recent activity log</h2><div class="tbl"><table><tbody>{log}</tbody></table></div></section>
</div></main><script>(function(){{var k='dashY';var y=sessionStorage.getItem(k);if(y!==null){{window.scrollTo(0,parseInt(y,10)||0);}}function save(){{sessionStorage.setItem(k,String(window.scrollY));}}window.addEventListener('beforeunload',save);document.addEventListener('submit',save,true);}})();</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
        try:
            body = render(collect()).encode()
            code = 200
        except Exception as e:  # keep the page up even if the DB is briefly locked
            body = f"<h1>Dashboard error</h1><pre>{esc(e)}</pre><meta http-equiv=refresh content=15>".encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """Veto / restore one idea. Guarded against other web pages posting to localhost: same-origin check plus a token."""
        from urllib.parse import parse_qs

        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        ok_origin = origin.startswith(f"http://127.0.0.1:{PORT}") or origin.startswith(f"http://localhost:{PORT}")
        length = min(int(self.headers.get("Content-Length") or 0), 2048)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "ignore"))
        field = lambda k: (form.get(k) or [""])[0]
        if self.path != "/idea" or not ok_origin or field("token") != TOKEN or field("action") not in ("veto", "restore") or not field("id").isdigit():
            self.send_error(403)
            return
        set_idea_status(int(field("id")), field("action"))
        self.send_response(303)
        self.send_header("Location", "/#ideas")
        self.end_headers()

    def log_message(self, *a) -> None:
        pass


if __name__ == "__main__":
    if "--once" in sys.argv:  # render to stdout, for testing
        print(render(collect()))
        raise SystemExit
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # localhost only
    print(f"Dashboard on http://127.0.0.1:{PORT}")
    srv.serve_forever()
