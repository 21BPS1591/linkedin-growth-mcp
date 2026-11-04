"""Local health dashboard for the LinkedIn Growth MCP. Read-only, with ONE exception: the idea Veto/Restore buttons
change an idea's status (backlog <-> dropped) in growth.db. Nothing is ever deleted and the server is never touched.

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
SAFE_ENV = {"DAILY_GEMINI_CALL_CAP": "16", "DAILY_BROWSER_PAGE_CAP": "30", "POST_HOUR": "9", "MAX_POSTS_PER_WEEK": "7"}


def env() -> dict[str, str]:
    out = dict(SAFE_ENV)
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
            k, _, v = line.partition("=")
            if k.strip() in SAFE_ENV and v.strip():
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


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
    cfg = env()
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
        post_hour = int(cfg["POST_HOUR"])
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
        b, bcap = usage.get("browser_page", 0), int(cfg["DAILY_BROWSER_PAGE_CAP"])
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
            SELECT p.id, p.url, p.published_at, d.style, d.hook,
                   (SELECT impressions FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) imp,
                   (SELECT reactions FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) rea,
                   (SELECT comments FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) com,
                   (SELECT reposts FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) rep,
                   (SELECT captured_at FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) cap
            FROM posts p JOIN drafts d ON d.id=p.draft_id ORDER BY p.published_at DESC""").fetchall()
        gone = set(kv(c, "ignored_posts") or [])  # deleted on LinkedIn: hidden here, rows stay in the DB
        posts = [p for p in posts if p["id"] not in gone]
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
            "ORDER BY status='dropped', priority, id LIMIT 60").fetchall()
        jobs = [("Autopilot cycle", "every 60 min"), ("Daily post slot", f"{post_hour}:00 (draft opens 30 min before)"),
                ("Metrics refresh", "every 6 h")]
        dbsize = (ROOT / "data" / "growth.db").stat().st_size / 1e6
    finally:
        c.close()
    return dict(keys=keys, weeks=weeks, all_fol=all_fol, checks=checks, overall=overall, posts=posts, followers=followers, log=log, ipos=ipos, counts=counts,
                jobs=jobs, errs=errs, dbsize=dbsize, now=now, ideas=ideas)


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


def render(d: dict) -> str:
    label = {"ok": "All systems working", "warn": "Working, needs a look", "bad": "Problem, action needed"}[d["overall"]]
    rows = "".join(f'<tr><td><span class="dot {s}"></span></td><td>{esc(n)}</td><td class="mut">{esc(t)}</td></tr>' for s, n, t in d["checks"])
    posts = "".join(
        f'<tr><td>{esc(stamp(p["published_at"]))}</td><td>{esc(p["style"])}</td>'
        f'<td class="hook">{"<a href=%s target=_blank>" % esc(p["url"]) if p["url"] else ""}{esc((p["hook"] or "")[:70])}{"</a>" if p["url"] else ""}</td>'
        f'<td class="n">{p["imp"] if p["imp"] is not None else "-"}</td><td class="n">{p["rea"] or 0}</td>'
        f'<td class="n">{p["com"] or 0}</td><td class="n">{p["rep"] or 0}</td><td class="mut">{esc(ago(p["cap"]))}</td></tr>'
        for p in d["posts"]) or '<tr><td colspan="8" class="mut">no posts yet</td></tr>'
    latest = d["followers"][0]["count"] if d["followers"] else 0

    def fol_row(r) -> str:  # a reading far from the newest one is a scrape error (an old parser once grabbed other accounts' counts)
        bad = latest and r["count"] > latest * 20
        note = ' <span class="mut">(scrape error, ignore)</span>' if bad else ""
        return f'<tr><td>{esc(r["day"])}</td><td class="n"{" style=text-decoration:line-through" if bad else ""}>{r["count"]:,}</td><td>{note}</td></tr>'

    fol = "".join(fol_row(r) for r in d["followers"])
    wk = list(d["weeks"].items())
    wrows = "".join(
        f'<tr><td>{esc(k)}</td><td class="n">{v["posts"]}</td><td class="n">{v["imp"] / v["posts"]:.0f}</td>'
        f'<td class="n">{v["rea"] / v["posts"]:.1f}</td><td class="n">{v["com"] / v["posts"]:.1f}</td></tr>' for k, v in wk)
    fr = d["all_fol"]
    if len(fr) >= 2 and fr[0]["count"]:
        delta = fr[-1]["count"] - fr[0]["count"]
        ftxt = f'{fr[0]["count"]:,} on {esc(fr[0]["day"])} to {fr[-1]["count"]:,} on {esc(fr[-1]["day"])} ({delta:+,})'
    elif fr:
        ftxt = f'baseline {fr[0]["count"]:,} on {esc(fr[0]["day"])}; the change shows once there is a second day'
    else:
        ftxt = "no follower reading yet"
    proof = (f'<p class="mut" style="margin:0 0 8px">Followers since baseline: <b style="color:var(--tx)">{ftxt}</b>. '
             f'The newest week is still maturing (posts keep collecting impressions for days), and a few posts per week '
             f'cannot prove a trend. Judge after 8 to 12 weeks.</p>'
             f'<table><tr><th>Week</th><th class="n">Posts</th><th class="n">Avg impressions</th><th class="n">Avg reactions</th><th class="n">Avg comments</th></tr>{wrows}</table>')
    log = "".join(
        f'<tr class="{"errrow" if "error" in r["action"] or r["action"] in ("blocked", "alert") else ""}">'
        f'<td class="mut nw">{esc(stamp(r["ts"]))}</td><td class="nw">{esc(r["action"])}</td><td class="mut">{esc(r["detail"][:160])}</td></tr>'
        for r in d["log"])
    ipo = "".join(
        f'<tr><td>{esc(r["company"].title())}</td><td>{"verified" if r["verified"] else "<span class=mut>unverified</span>"}</td></tr>'
        for r in d["ipos"])
    def idea_row(r) -> str:
        veto = r["status"] == "backlog"
        btn = (f'<form method="post" action="/idea" style="margin:0"><input type="hidden" name="token" value="{TOKEN}">'
               f'<input type="hidden" name="id" value="{r["id"]}"><input type="hidden" name="action" value="{"veto" if veto else "restore"}">'
               f'<button class="vbtn{"" if veto else " rest"}">{"Veto" if veto else "Restore"}</button></form>')
        return (f'<tr class="{"" if veto else "mutrow"}"><td class="nw">{esc(r["series"])}</td><td>{esc(r["title"])}</td>'
                f'<td class="mut nw">{"queued" if veto else "vetoed"}</td><td>{btn}</td></tr>')

    ideas = "".join(idea_row(r) for r in d["ideas"]) or '<tr><td colspan="4" class="mut">no ideas in the backlog</td></tr>'
    stats = "".join(f'<div class="stat"><b>{v:,}</b><span>{esc(k)}</span></div>' for k, v in d["counts"].items())
    jobs = "".join(f'<tr><td>{esc(a)}</td><td class="mut">{esc(b)}</td></tr>' for a, b in d["jobs"])
    keyrows = "".join(
        f'<tr><td>{esc(k["purpose"])}</td><td>{esc(k["account"])}</td><td>{"off on purpose" if not k.get("enabled", True) else "set" if k["configured"] else "<b>missing</b>"}</td>'
        f'<td class="mut">{esc(k["does"])}</td></tr>' for k in d["keys"]) or "<tr><td colspan=4 class=mut>restart the server once to publish this</td></tr>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>LinkedIn Growth: platform health</title><style>
:root{{--bg:#0f1419;--card:#18202a;--line:#263241;--tx:#e6edf3;--mut:#8b98a8;--ok:#3fb950;--warn:#d29922;--bad:#f85149}}
@media(prefers-color-scheme:light){{:root{{--bg:#f4f6f8;--card:#fff;--line:#dde3ea;--tx:#1b2430;--mut:#667385}}}}
*{{box-sizing:border-box}}body{{margin:0;font:14px/1.45 system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--tx)}}
main{{max-width:1180px;margin:0 auto;padding:20px}}
.banner{{display:flex;align-items:center;gap:14px;padding:18px 20px;border-radius:12px;background:var(--card);border:1px solid var(--line);border-left:6px solid var(--{d["overall"]})}}
.banner h1{{margin:0;font-size:20px}}.banner small{{color:var(--mut)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-top:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;overflow:auto}}.wide{{grid-column:1/-1}}
h2{{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}}
table{{width:100%;border-collapse:collapse}}td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{color:var(--mut);font-weight:600;font-size:12px}}tr:last-child td{{border-bottom:0}}.n{{text-align:right;font-variant-numeric:tabular-nums}}
.mut{{color:var(--mut)}}.nw{{white-space:nowrap}}.hook a{{color:inherit}}.errrow td{{background:color-mix(in srgb,var(--bad) 12%,transparent)}}
.dot{{display:inline-block;width:11px;height:11px;border-radius:50%}}.dot.ok{{background:var(--ok)}}.dot.warn{{background:var(--warn)}}.dot.bad{{background:var(--bad)}}
.stats{{display:flex;flex-wrap:wrap;gap:10px}}.stat{{flex:1 1 130px;border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.vbtn{{background:transparent;color:var(--bad);border:1px solid var(--bad);border-radius:6px;padding:3px 10px;cursor:pointer;font:inherit}}.vbtn.rest{{color:var(--ok);border-color:var(--ok)}}.mutrow td{{opacity:.55}}
.stat b{{display:block;font-size:20px;font-variant-numeric:tabular-nums}}.stat span{{color:var(--mut);font-size:12px}}
</style></head><body><main>
<div class="banner"><span class="dot {d["overall"]}" style="width:22px;height:22px"></span>
<div><h1>{label}</h1><small>Updated {esc(datetime.fromtimestamp(d["now"]).strftime("%d %b %Y %H:%M:%S"))} · auto-refresh 60 s · DB {d["dbsize"]:.1f} MB · read-only view</small></div></div>
<div class="grid">
<section class="card"><h2>Health checks</h2><table>{rows}</table></section>
<section class="card"><h2>Scheduled jobs</h2><table>{jobs}</table><h2 style="margin-top:16px">Data kept (never deleted)</h2><div class="stats">{stats}</div></section>
<section class="card wide"><h2>Posts and latest metrics</h2><table><tr><th>Published</th><th>Style</th><th>Hook</th><th class="n">Impr.</th><th class="n">Reactions</th><th class="n">Comments</th><th class="n">Reposts</th><th>Read</th></tr>{posts}</table></section>
<section class="card wide"><h2>Growth proof (vs your starting point)</h2>{proof}</section>
<section class="card wide"><h2>Gemini keys: which account does which job</h2><table><tr><th>Job</th><th>Google account</th><th>Key</th><th>Used for</th></tr>{keyrows}</table></section>
<section class="card"><h2>Followers (last 7 readings)</h2><table>{fol}</table></section>
<section class="card"><h2>Tracked IPOs</h2><table>{ipo}</table></section>
<section class="card wide" id="ideas"><h2>Post ideas: veto what you do not want posted</h2>
<p class="mut" style="margin:0 0 8px">Queued ideas are posted in the order the topic mix picks them. Veto keeps the idea (nothing is deleted) but the autopilot will never use it; Restore puts it back.</p>
<table><tr><th>Series</th><th>Idea</th><th>State</th><th></th></tr>{ideas}</table></section>
<section class="card wide"><h2>Recent activity log</h2><table>{log}</table></section>
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
