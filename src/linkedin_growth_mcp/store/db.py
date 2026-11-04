"""SQLite store: interests, profile snapshot, drafts, posts, metrics, usage counters."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    style TEXT NOT NULL,
    hook TEXT NOT NULL,
    text TEXT NOT NULL,
    image_prompt TEXT,
    image_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id),
    urn TEXT NOT NULL,
    url TEXT,
    published_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    checkpoint TEXT NOT NULL,
    impressions INTEGER NOT NULL DEFAULT 0,
    reactions INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    reposts INTEGER NOT NULL DEFAULT 0,
    captured_at REAL NOT NULL,
    UNIQUE(post_id, checkpoint)
);
-- Daily follower count: the account-level growth number, append-only (one row per day).
CREATE TABLE IF NOT EXISTS followers (
    day TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    captured_at REAL NOT NULL
);
-- Quality of the conversation under each post: comments received and the owner's own replies. Append-only.
CREATE TABLE IF NOT EXISTS conversation_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    captured_at REAL NOT NULL,
    comments INTEGER NOT NULL DEFAULT 0,
    author_replies INTEGER NOT NULL DEFAULT 0,
    reply_rate REAL,
    comment_quality REAL,
    reply_quality REAL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_tags (
    post_id INTEGER NOT NULL REFERENCES posts(id),
    tag TEXT NOT NULL,
    PRIMARY KEY (post_id, tag)
);
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series TEXT NOT NULL,
    title TEXT NOT NULL UNIQUE,
    brief TEXT NOT NULL,
    facts TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'backlog',
    draft_id INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ipos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    source_file TEXT NOT NULL,
    data TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    rule TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    source_post_id INTEGER,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS post_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    checkpoint TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(post_id, checkpoint)
);
CREATE TABLE IF NOT EXISTS autopilot_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ipo_id INTEGER,
    step TEXT,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    draft_id INTEGER
);
CREATE TABLE IF NOT EXISTS post_audience (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    pct REAL,
    num REAL,
    captured_at REAL NOT NULL,
    UNIQUE(post_id, snapshot, category, value)
);
-- Append-only history of every metrics reading (never updated or deleted); feeds dashboards and ML.
CREATE TABLE IF NOT EXISTS metrics_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    checkpoint TEXT,
    impressions INTEGER NOT NULL DEFAULT 0,
    reactions INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    reposts INTEGER NOT NULL DEFAULT 0,
    captured_at REAL NOT NULL
);
-- Supplementary RHP facts and pre-IPO investors, kept apart from the verified extraction. Append-only.
CREATE TABLE IF NOT EXISTS ipo_extra (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ipo_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    value_num REAL,
    value_text TEXT,
    page INTEGER,
    source TEXT NOT NULL,
    captured_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ipo_investors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ipo_id INTEGER NOT NULL,
    investor TEXT NOT NULL,
    round_type TEXT NOT NULL,
    allotment_date TEXT,
    shares INTEGER,
    price_per_share REAL,
    page INTEGER,
    source TEXT NOT NULL,
    captured_at REAL NOT NULL,
    UNIQUE(ipo_id, investor, round_type, allotment_date)
);
-- Market-wide IPO data from chittorgarh.com (secondary source). Facts are append-only: a new row is added only when a value changes.
CREATE TABLE IF NOT EXISTS cg_ipos (
    cg_id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    company TEXT NOT NULL,
    url TEXT NOT NULL,
    dates TEXT,
    first_seen REAL NOT NULL,
    last_fetched REAL,
    last_subscription REAL
);
CREATE TABLE IF NOT EXISTS cg_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cg_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    value_num REAL,
    value_text TEXT,
    captured_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cg_facts_idx ON cg_facts(cg_id, field, captured_at);
CREATE TABLE IF NOT EXISTS cg_investors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cg_id INTEGER NOT NULL,
    investor TEXT NOT NULL,
    round_type TEXT NOT NULL,
    shares INTEGER,
    price_per_share REAL,
    amount_cr REAL,
    source TEXT NOT NULL,
    captured_at REAL NOT NULL,
    UNIQUE(cg_id, investor, round_type)
);
-- Listing-day prices from the exchanges' own files (primary source). Never updated or deleted.
CREATE TABLE IF NOT EXISTS exchange_listing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cg_id INTEGER NOT NULL,
    exchange TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    symbol TEXT,
    isin TEXT,
    open REAL, high REAL, low REAL, close REAL, last REAL, prev_close REAL, volume REAL,
    match_score REAL,
    source TEXT NOT NULL,
    captured_at REAL NOT NULL,
    UNIQUE(cg_id, exchange, trade_date)
);
CREATE TABLE IF NOT EXISTS usage (day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY(day, kind));
"""


class Store:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # --- key/value (interests, profile snapshot) ---
    def set_kv(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )
        self.conn.commit()

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # --- drafts ---
    def add_draft(self, style: str, hook: str, text: str, image_prompt: str | None) -> int:
        from ..linkedin.publish import strip_dashes

        text = strip_dashes(text)
        cur = self.conn.execute(
            "INSERT INTO drafts(style,hook,text,image_prompt,created_at) VALUES(?,?,?,?,?)",
            (style, hook, text, image_prompt, time.time()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()

    def update_draft(self, draft_id: int, **fields: Any) -> None:
        allowed = {"text", "image_prompt", "image_path", "status"}
        if isinstance(fields.get("text"), str):
            from ..linkedin.publish import strip_dashes

            fields["text"] = strip_dashes(fields["text"])
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE drafts SET {cols} WHERE id=?", (*fields.values(), draft_id))
        self.conn.commit()

    # --- posts / metrics ---
    def add_post(self, draft_id: int, urn: str, url: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO posts(draft_id,urn,url,published_at) VALUES(?,?,?,?)",
            (draft_id, urn, url, time.time()),
        )
        self.conn.execute("UPDATE drafts SET status='published' WHERE id=?", (draft_id,))
        self.conn.commit()
        return int(cur.lastrowid)

    def posts_published_since(self, since_ts: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE published_at>=?", (since_ts,)
        ).fetchone()
        return int(row["c"])

    def list_posts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.*, d.style, d.hook, d.text FROM posts p JOIN drafts d ON d.id=p.draft_id "
            "ORDER BY p.published_at"
        ).fetchall()

    def log_metrics(self, post_id: int, source: str, checkpoint: str | None, impressions: int,
                    reactions: int, comments: int, reposts: int) -> None:
        self.conn.execute(
            "INSERT INTO metrics_log(post_id,source,checkpoint,impressions,reactions,comments,reposts,captured_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (post_id, source, checkpoint, impressions, reactions, comments, reposts, time.time()),
        )
        self.conn.commit()

    def save_metrics(
        self, post_id: int, checkpoint: str, impressions: int, reactions: int, comments: int, reposts: int
    ) -> None:
        self.log_metrics(post_id, "checkpoint", checkpoint, impressions, reactions, comments, reposts)
        self.conn.execute(
            "INSERT INTO metrics(post_id,checkpoint,impressions,reactions,comments,reposts,captured_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(post_id,checkpoint) DO UPDATE SET "
            "impressions=excluded.impressions, reactions=excluded.reactions, "
            "comments=excluded.comments, reposts=excluded.reposts, captured_at=excluded.captured_at",
            (post_id, checkpoint, impressions, reactions, comments, reposts, time.time()),
        )
        self.conn.commit()

    def save_audience(self, post_id: int, snapshot: str, rows: list[tuple[str, str, float | None, float | None]]) -> None:
        """rows: (category, value, pct, num). Re-importing the same snapshot day overwrites it."""
        for category, value, pct, num in rows:
            self.conn.execute(
                "INSERT INTO post_audience(post_id,snapshot,category,value,pct,num,captured_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(post_id,snapshot,category,value) DO UPDATE SET pct=excluded.pct, num=excluded.num, "
                "captured_at=excluded.captured_at",
                (post_id, snapshot, category, value, pct, num, time.time()),
            )
        self.conn.commit()

    def audience_rows(self, post_id: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT post_id,snapshot,category,value,pct,num FROM post_audience"
        args: tuple = ()
        if post_id is not None:
            q += " WHERE post_id=?"
            args = (post_id,)
        return self.conn.execute(q + " ORDER BY post_id, snapshot, category, pct DESC", args).fetchall()

    def add_ipo_extra(self, ipo_id: int, field: str, value_num: float | None, value_text: str | None,
                      page: int | None, source: str) -> None:
        self.conn.execute(
            "INSERT INTO ipo_extra(ipo_id,field,value_num,value_text,page,source,captured_at) VALUES(?,?,?,?,?,?,?)",
            (ipo_id, field, value_num, value_text, page, source, time.time()))
        self.conn.commit()

    def add_ipo_investor(self, ipo_id: int, investor: str, round_type: str, allotment_date: str | None,
                         shares: int | None, price: float | None, page: int | None, source: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO ipo_investors(ipo_id,investor,round_type,allotment_date,shares,price_per_share,page,source,captured_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (ipo_id, investor, round_type, allotment_date, shares, price, page, source, time.time()))
        self.conn.commit()

    def latest_ipo_extra(self, ipo_id: int) -> dict[str, sqlite3.Row]:
        out: dict[str, sqlite3.Row] = {}
        for r in self.conn.execute("SELECT * FROM ipo_extra WHERE ipo_id=? ORDER BY captured_at, id", (ipo_id,)):
            out[r["field"]] = r
        return out

    def ipo_investor_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM ipo_investors ORDER BY ipo_id, allotment_date, shares DESC").fetchall()

    def upsert_cg_ipo(self, cg_id: int, slug: str, company: str, url: str, dates: str) -> None:
        self.conn.execute(
            "INSERT INTO cg_ipos(cg_id,slug,company,url,dates,first_seen) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(cg_id) DO UPDATE SET company=excluded.company, url=excluded.url, dates=excluded.dates",
            (cg_id, slug, company, url, dates, time.time()))
        self.conn.commit()

    def cg_ipo_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM cg_ipos ORDER BY cg_id").fetchall()

    def touch_cg_ipo(self, cg_id: int, subscription: bool = False) -> None:
        col = "last_subscription" if subscription else "last_fetched"
        self.conn.execute(f"UPDATE cg_ipos SET {col}=? WHERE cg_id=?", (time.time(), cg_id))
        self.conn.commit()

    def add_cg_facts(self, cg_id: int, facts: list[dict[str, Any]]) -> int:
        """Append only the facts whose value changed since the last stored reading. Returns how many rows were added."""
        added = 0
        now = time.time()
        for f in facts:
            last = self.conn.execute(
                "SELECT value_text FROM cg_facts WHERE cg_id=? AND field=? ORDER BY captured_at DESC, id DESC LIMIT 1",
                (cg_id, f["field"])).fetchone()
            if last is not None and last["value_text"] == f["text"]:
                continue
            self.conn.execute("INSERT INTO cg_facts(cg_id,field,value_num,value_text,captured_at) VALUES(?,?,?,?,?)",
                              (cg_id, f["field"], f["num"], f["text"], now))
            added += 1
        self.conn.commit()
        return added

    def add_cg_investor(self, cg_id: int, investor: str, round_type: str, shares: int | None,
                        price: float | None, amount_cr: float | None, source: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO cg_investors(cg_id,investor,round_type,shares,price_per_share,amount_cr,source,captured_at) "
            "VALUES(?,?,?,?,?,?,?,?)", (cg_id, investor, round_type, shares, price, amount_cr, source, time.time()))
        self.conn.commit()

    def has_cg_investors(self, cg_id: int, prefix: str) -> bool:
        return self.conn.execute("SELECT 1 FROM cg_investors WHERE cg_id=? AND round_type LIKE ? LIMIT 1",
                                 (cg_id, prefix + "%")).fetchone() is not None

    def add_exchange_listing(self, cg_id: int, exchange: str, trade_date: str, v: dict[str, Any], source: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO exchange_listing(cg_id,exchange,trade_date,symbol,isin,open,high,low,close,last,prev_close,volume,match_score,source,captured_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cg_id, exchange, trade_date, v.get("symbol"), v.get("isin"), v.get("open"), v.get("high"), v.get("low"), v.get("close"),
             v.get("last"), v.get("prev_close"), v.get("volume"), v.get("match_score"), source, time.time()))
        self.conn.commit()

    def exchange_listing_rows(self, cg_id: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM exchange_listing" + (" WHERE cg_id=?" if cg_id is not None else "") + " ORDER BY trade_date, cg_id, exchange"
        return self.conn.execute(q, (cg_id,) if cg_id is not None else ()).fetchall()

    def latest_metrics(self) -> list[sqlite3.Row]:
        """Most recent metrics snapshot per post, joined with style/hook."""
        return self.conn.execute(
            "SELECT p.id post_id, p.published_at, d.style, d.hook, m.impressions, m.reactions, "
            "m.comments, m.reposts FROM posts p JOIN drafts d ON d.id=p.draft_id "
            "JOIN metrics m ON m.id=(SELECT id FROM metrics WHERE post_id=p.id ORDER BY captured_at DESC LIMIT 1)"
        ).fetchall()

    def post_texts(self) -> list[sqlite3.Row]:
        """Published post text with its latest metrics, oldest first (read-only; feeds the hook report and experiments)."""
        return self.conn.execute(
            "SELECT p.id post_id, p.published_at, d.text, m.impressions, m.reactions, m.comments, m.reposts "
            "FROM posts p JOIN drafts d ON d.id=p.draft_id "
            "JOIN metrics m ON m.id=(SELECT id FROM metrics WHERE post_id=p.id ORDER BY captured_at DESC LIMIT 1) "
            "ORDER BY p.published_at"
        ).fetchall()

    def post_metrics_72h(self) -> list[sqlite3.Row]:
        """Each post at its 72h checkpoint (same age for every post), oldest first, with its local posting hour."""
        return self.conn.execute(
            "SELECT p.id post_id, p.published_at, CAST(strftime('%H', p.published_at, 'unixepoch', 'localtime') AS INTEGER) hour, "
            "m.impressions, m.reactions, m.comments, m.reposts FROM posts p "
            "JOIN metrics m ON m.post_id=p.id AND m.checkpoint='72h' ORDER BY p.published_at"
        ).fetchall()

    def post_kinds(self) -> dict[int, str]:
        """'ipo' for posts drafted from an IPO idea, 'pillar' for everything else (work stories, BI tips)."""
        rows = self.conn.execute(
            "SELECT p.id post_id, i.series series FROM posts p LEFT JOIN ideas i ON i.draft_id=p.draft_id").fetchall()
        return {r["post_id"]: "ipo" if r["series"] == "ipo" else "pillar" for r in rows}

    def series_metrics(self) -> list[sqlite3.Row]:
        """Latest metrics per post with the backlog series it was drafted from (posts without an idea are left out)."""
        return self.conn.execute(
            "SELECT p.id post_id, p.published_at, i.series, m.impressions, m.reactions, m.comments, m.reposts "
            "FROM posts p JOIN ideas i ON i.draft_id=p.draft_id "
            "JOIN metrics m ON m.id=(SELECT id FROM metrics WHERE post_id=p.id ORDER BY captured_at DESC LIMIT 1)"
        ).fetchall()

    def hour_scores(self) -> list[sqlite3.Row]:
        """Latest metrics per post plus the local hour it was published, for the posting-time bandit."""
        return self.conn.execute(
            "SELECT p.published_at, m.impressions, m.reactions, m.comments, m.reposts FROM posts p "
            "JOIN metrics m ON m.id=(SELECT id FROM metrics WHERE post_id=p.id ORDER BY captured_at DESC LIMIT 1)"
        ).fetchall()

    # --- followers ---
    def save_followers(self, day: str, count: int) -> None:
        self.conn.execute(
            "INSERT INTO followers(day,count,captured_at) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET count=excluded.count, captured_at=excluded.captured_at",
            (day, count, time.time()),
        )
        self.conn.commit()

    def follower_rows(self, limit: int = 90) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM followers ORDER BY day DESC LIMIT ?", (limit,)).fetchall()

    # --- conversation quality ---
    def add_conversation(self, post_id: int, a: dict[str, Any]) -> int:
        rate = (a["author_replies"] / a["comments"]) if a["comments"] else None
        cur = self.conn.execute(
            "INSERT INTO conversation_quality(post_id,captured_at,comments,author_replies,reply_rate,"
            "comment_quality,reply_quality,detail) VALUES(?,?,?,?,?,?,?,?)",
            (post_id, time.time(), a["comments"], a["author_replies"], rate,
             a["comment_quality"], a["reply_quality"], json.dumps(a)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest_conversation(self, post_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM conversation_quality WHERE post_id=? ORDER BY captured_at DESC LIMIT 1", (post_id,)
        ).fetchone()

    def conversation_rows(self, limit: int = 50) -> list[sqlite3.Row]:
        """Latest reading per post, newest post first."""
        return self.conn.execute(
            "SELECT c.* FROM conversation_quality c WHERE c.id=("
            "  SELECT id FROM conversation_quality WHERE post_id=c.post_id ORDER BY captured_at DESC LIMIT 1)"
            " ORDER BY c.post_id DESC LIMIT ?", (limit,)
        ).fetchall()

    def add_post_tags(self, post_id: int, tags: list[str]) -> None:
        self.conn.executemany("INSERT OR IGNORE INTO post_tags(post_id,tag) VALUES(?,?)", [(post_id, t) for t in tags])
        self.conn.commit()

    def tag_stats(self) -> dict[str, tuple[int, float]]:
        """tag -> (posts using it, mean latest impressions), from posts that have a metrics snapshot."""
        rows = self.conn.execute(
            "SELECT t.tag, COUNT(*) n, AVG(m.impressions) avg_imp FROM post_tags t "
            "JOIN metrics m ON m.id=(SELECT id FROM metrics WHERE post_id=t.post_id ORDER BY captured_at DESC LIMIT 1) "
            "GROUP BY t.tag"
        ).fetchall()
        return {r["tag"]: (int(r["n"]), float(r["avg_imp"])) for r in rows}

    # --- content ideas ---
    def seed_ideas(self, ideas: list[tuple[str, str, str, int]]) -> int:
        """Insert starter ideas that are not already present (matched by title). Returns rows added."""
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO ideas(series,title,brief,priority,created_at) VALUES(?,?,?,?,?)",
            [(s, t, b, p, time.time()) for s, t, b, p in ideas],
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def add_idea(self, series: str, title: str, brief: str, priority: int = 2, facts: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO ideas(series,title,brief,facts,priority,created_at) VALUES(?,?,?,?,?,?)",
            (series, title, brief, facts, priority, time.time()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_ideas(self, series: str | None = None, status: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM ideas WHERE 1=1", []
        if series:
            q += " AND series=?"
            args.append(series)
        if status:
            q += " AND status=?"
            args.append(status)
        return self.conn.execute(q + " ORDER BY priority, id", args).fetchall()

    def get_idea(self, idea_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()

    def update_idea(self, idea_id: int, **fields: Any) -> None:
        allowed = {"series", "title", "brief", "facts", "priority", "status", "draft_id"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE ideas SET {cols} WHERE id=?", (*fields.values(), idea_id))
        self.conn.commit()

    # --- IPO filings ---
    def add_ipo(self, company: str, source_file: str, data: dict[str, Any]) -> int:
        """One row per source file: re-ingesting the same RHP replaces the earlier extraction."""
        old = self.conn.execute("SELECT id FROM ipos WHERE source_file=?", (source_file,)).fetchone()
        if old:
            self.conn.execute("UPDATE ipos SET company=?, data=?, verified=0, created_at=? WHERE id=?",
                              (company, json.dumps(data), time.time(), old["id"]))
            self.conn.commit()
            return int(old["id"])
        cur = self.conn.execute(
            "INSERT INTO ipos(company,source_file,data,created_at) VALUES(?,?,?,?)",
            (company, source_file, json.dumps(data), time.time()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_ipo(self, ipo_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM ipos WHERE id=?", (ipo_id,)).fetchone()

    def update_ipo(self, ipo_id: int, data: dict[str, Any] | None = None, verified: bool | None = None) -> None:
        if data is not None:
            self.conn.execute("UPDATE ipos SET data=?, verified=0 WHERE id=?", (json.dumps(data), ipo_id))
        if verified is not None:
            self.conn.execute("UPDATE ipos SET verified=? WHERE id=?", (int(verified), ipo_id))
        self.conn.commit()

    # --- audience feedback ---
    MAX_ACTIVE_RULES = 25

    def upsert_rule(self, kind: str, rule: str, post_id: int | None) -> int:
        """Add a writing rule learned from comments, or strengthen it if the same rule was learned before."""
        now = time.time()
        row = self.conn.execute("SELECT id FROM feedback_rules WHERE lower(rule)=lower(?)", (rule,)).fetchone()
        if row:
            self.conn.execute("UPDATE feedback_rules SET hits=hits+1, last_seen=?, active=1 WHERE id=?", (now, row["id"]))
            rid = int(row["id"])
        else:
            cur = self.conn.execute(
                "INSERT INTO feedback_rules(kind,rule,source_post_id,created_at,last_seen) VALUES(?,?,?,?,?)",
                (kind, rule, post_id, now, now),
            )
            rid = int(cur.lastrowid)
        # keep the list short: retire the weakest, oldest rules beyond the cap
        self.conn.execute(
            "UPDATE feedback_rules SET active=0 WHERE active=1 AND id NOT IN ("
            "SELECT id FROM feedback_rules WHERE active=1 ORDER BY hits DESC, last_seen DESC LIMIT ?)",
            (self.MAX_ACTIVE_RULES,),
        )
        self.conn.commit()
        return rid

    def reinforce_rule(self, rule_id: int) -> None:
        self.conn.execute("UPDATE feedback_rules SET hits=hits+1, last_seen=? WHERE id=? AND active=1", (time.time(), rule_id))
        self.conn.commit()

    def active_rules(self, limit: int = 8) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM feedback_rules WHERE active=1 ORDER BY hits DESC, last_seen DESC LIMIT ?", (limit,)
        ).fetchall()

    def retire_rule(self, rule_id: int) -> None:
        self.conn.execute("UPDATE feedback_rules SET active=0 WHERE id=?", (rule_id,))
        self.conn.commit()

    def save_post_feedback(self, post_id: int, checkpoint: str, summary: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO post_feedback(post_id,checkpoint,summary,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(post_id,checkpoint) DO UPDATE SET summary=excluded.summary, created_at=excluded.created_at",
            (post_id, checkpoint, json.dumps(summary), time.time()),
        )
        self.conn.commit()

    def has_post_feedback(self, post_id: int, checkpoint: str) -> bool:
        return self.conn.execute("SELECT 1 FROM post_feedback WHERE post_id=? AND checkpoint=?", (post_id, checkpoint)).fetchone() is not None

    def recent_feedback(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM post_feedback ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # --- autopilot log ---
    def add_log(self, action: str, detail: str = "", ipo_id: int | None = None, step: str | None = None,
                draft_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO autopilot_log(ts,ipo_id,step,action,detail,draft_id) VALUES(?,?,?,?,?,?)",
            (time.time(), ipo_id, step, action, detail[:2000], draft_id),
        )
        self.conn.commit()

    def list_logs(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM autopilot_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def step_logged(self, ipo_id: int, step: str, actions: tuple[str, ...], since_ts: float = 0.0) -> bool:
        marks = ",".join("?" * len(actions))
        row = self.conn.execute(
            f"SELECT 1 FROM autopilot_log WHERE ipo_id=? AND step=? AND action IN ({marks}) AND ts>=? LIMIT 1",
            (ipo_id, step, *actions, since_ts),
        ).fetchone()
        return row is not None

    def list_ipos(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM ipos ORDER BY id").fetchall()

    def set_ipo_data(self, ipo_id: int, data: dict[str, Any]) -> None:
        """Save enrichment (business line, price band ...) without touching the verified flag."""
        self.conn.execute("UPDATE ipos SET data=? WHERE id=?", (json.dumps(data), ipo_id))
        self.conn.commit()

    # --- usage caps ---
    def usage_today(self, kind: str) -> int:
        day = time.strftime("%Y-%m-%d")
        row = self.conn.execute("SELECT n FROM usage WHERE day=? AND kind=?", (day, kind)).fetchone()
        return int(row["n"]) if row else 0

    def bump_usage(self, kind: str, cap: int) -> None:
        """Increment today's counter, raising if the cap would be exceeded."""
        if self.usage_today(kind) >= cap:
            raise RuntimeError(f"daily cap reached for '{kind}' ({cap})")
        day = time.strftime("%Y-%m-%d")
        self.conn.execute(
            "INSERT INTO usage(day,kind,n) VALUES(?,?,1) ON CONFLICT(day,kind) DO UPDATE SET n=n+1",
            (day, kind),
        )
        self.conn.commit()
