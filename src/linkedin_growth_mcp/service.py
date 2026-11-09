"""Business logic behind the MCP tools. Kept free of transport concerns so it is unit-testable."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from statistics import mean
from typing import Any

from . import feedback
from .config import Config
from .linkedin import analytics, oauth, profile, publish
from .linkedin.browser import LinkedInBrowser, LoginExpired
from .store.db import Store
from .strategy import formulas, ownvoice
from .strategy.bandit import StyleBandit
from .strategy.hashtags import pick_hashtags
from .strategy.ideas_seed import SEED_IDEAS
from .strategy.scoring import external_reactions, post_score
from .strategy.styles import arm_key

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
WEEK = 7 * 24 * 3600
CHECKPOINTS = [("24h", 24), ("72h", 72), ("7d", 168)]
DEFAULT_BASELINE_IMPRESSIONS = 500.0

INTEREST_QUESTIONS = [
    "Which 2-4 topics do you want to be known for on LinkedIn?",
    "Who exactly is your target audience (roles, industries, seniority)?",
    "What is the goal: followers, inbound clients, job offers, hiring, or thought leadership?",
    "How would you describe your voice (e.g. direct, warm, technical, humorous)?",
    "Which topics or opinions are off-limits (employer confidentiality, politics, etc.)?",
    "Name 2-3 creators or posts you admire, and what you like about them.",
    "How many posts per week can you realistically sustain?",
]


def _norm_company(name: str) -> str:
    import re

    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"\b(limited|ltd|pvt|private)\b", "", n)
    return re.sub(r"[^a-z0-9]", "", n)


class Service:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.browser = LinkedInBrowser(cfg, store)
        self._text = None
        self._insights_text = None
        self._image = None
        self._clients: dict[str, Any] = {}
        store.seed_ideas(SEED_IDEAS)

    @property
    def autopilot(self):
        if getattr(self, '_autopilot', None) is None:
            from .autopilot import Autopilot

            self._autopilot = Autopilot(self)
        return self._autopilot

    # Gemini clients are created lazily so the server starts without a key (e.g. for --login).
    @property
    def text(self):
        if self._text is None:
            from .gemini.text import GeminiText

            self._text = GeminiText(self.cfg, self.store)
        return self._text

    @property
    def insights_text(self):
        """Gemini text client on the insights key (profile fit, performance summary)."""
        if self._insights_text is None:
            from .gemini.text import GeminiText

            self._insights_text = GeminiText(self.cfg, self.store, "insights")
        return self._insights_text

    def gemini_client(self, purpose: str):
        """A client on the key for this kind of work. Raises KeyMissing when that job has no key: never another job's key."""
        from .gemini.keys import KeyMissing

        if not self.cfg.key_for(purpose):
            raise KeyMissing(purpose)
        if purpose not in self._clients:
            from google import genai
            from google.genai import types

            self._clients[purpose] = genai.Client(api_key=self.cfg.key_for(purpose), http_options=types.HttpOptions(timeout=180_000))
        return self._clients[purpose]

    def spend(self, purpose: str) -> None:
        """Count one request against this job's own key (raises when its daily cap is reached or it has no key)."""
        from .gemini.keys import KeyMissing

        if not self.cfg.key_for(purpose):
            raise KeyMissing(purpose)
        self.store.bump_usage(self.cfg.usage_kind(purpose), self.cfg.cap_for(purpose))
        from .gemini.keys import throttle

        throttle(self.cfg.usage_kind(purpose))

    def api_keys_report(self) -> dict[str, Any]:
        """Which Google account owns each Gemini key, what each is used for, and today's use. Never shows a key."""
        from .gemini.keys import MODEL_LIMITS, describe, rpm_guard

        keys = describe(self.cfg, self.store)
        c = self.cfg
        return {"models": {"post drafting": [c.draft_model, c.gemini_draft_fallback_model or None],
                           "editor check, hook ideas": c.gemini_text_model,
                           "reading IPO filings": [c.extraction_model, c.gemini_extraction_fallback_model or None],
                           "comment analysis, profile fit, summaries": c.gemini_text_model,
                           "images": c.gemini_image_model + " (off)"},
                "keys": keys, "paused": [k["purpose"] for k in keys if not k["configured"]],
                "model_limits": {m: {**lim, "rpm_guard": rpm_guard(m)} for m, lim in MODEL_LIMITS.items()},
                "model_limits_note": "limits are per Google project, read in AI Studio; a model with a small daily limit is skipped for its fallback once this system has used limit-1 requests",
                "note": "Each key only does its own job; a job without a key is paused, never moved to another key."}

    @property
    def image(self):
        if self._image is None:
            from .gemini.image import GeminiImage

            self._image = GeminiImage(self.cfg, self.store)
        return self._image

    # --- profile & interests ---
    async def sync_profile(self) -> dict[str, Any]:
        snap = await profile.scrape_own_profile(self.browser)
        self.store.set_kv("profile", snap)
        return {"chars": len(snap["raw_text"]), "preview": snap["raw_text"][:600]}

    def interest_questions(self) -> list[str]:
        return INTEREST_QUESTIONS

    def save_interests(self, interests: dict[str, Any]) -> dict[str, Any]:
        self.store.set_kv("interests", interests)
        banned = interests.get("banned_terms")
        if isinstance(banned, str):
            banned = [t.strip() for t in re.split(r"[,\n;]", banned)]
        if isinstance(banned, list):  # deterministic off-limits list, enforced by the gates (no model call)
            self.store.set_kv("banned_terms", [str(t).strip() for t in banned if str(t).strip()])
        return {"saved": True, "keys": sorted(interests)}

    # --- story interview, voice, profile scorecard (all deterministic, no model calls) ---
    def story_interview_questions(self) -> list[str]:
        return [q for _, q in ownvoice.SPINE_QUESTIONS]

    def save_story_interview(self, title: str, moment: str = "", number: str = "", correction: str = "",
                             opposition: str = "", ask: str = "", series: str = "story", priority: int = 2) -> dict[str, Any]:
        brief, facts = ownvoice.interview_to_idea(title, {"moment": moment, "number": number, "correction": correction,
                                                          "opposition": opposition, "ask": ask})
        return self.add_idea(series, title, brief, priority, facts)

    def save_voice_samples(self, samples: list[str]) -> dict[str, Any]:
        fp = ownvoice.voice_fingerprint(samples)
        if not fp:
            raise RuntimeError("Give at least one text you typed yourself")
        self.store.set_kv("voice_fingerprint", fp)
        return fp

    def profile_scorecard(self) -> dict[str, Any]:
        prof = self.store.get_kv("profile")
        if not prof:
            raise RuntimeError("Run sync_profile first")
        card = ownvoice.profile_scorecard(prof.get("raw_text", ""))
        self.store.set_kv("profile_scorecard", card | {"at": time.time()})
        return card

    def assess_fit(self) -> dict[str, Any]:
        prof, ints = self.store.get_kv("profile"), self.store.get_kv("interests")
        if not prof or not ints:
            raise RuntimeError("Need both sync_profile and save_interests before assess_fit")
        result = self.insights_text.assess_fit(prof, ints)
        self.store.set_kv("fit", result)
        return result

    # --- drafting ---
    def _history_scores(self) -> tuple[list[Any], list[tuple[str, str, float]], float]:
        rows = self.store.latest_metrics()
        impressions = [r["impressions"] for r in rows]
        baseline = mean(impressions) if impressions else DEFAULT_BASELINE_IMPRESSIONS
        scored = [
            (r["style"], r["hook"], post_score(r["impressions"], r["reactions"], r["comments"], r["reposts"], baseline))
            for r in rows
        ]
        return rows, scored, baseline

    # --- content backlog ---
    def list_ideas(self, series: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return [
            {k: r[k] for k in ("id", "series", "title", "priority", "status", "draft_id")} | {"has_facts": bool(r["facts"])}
            for r in self.store.list_ideas(series, status)
        ]

    def add_idea(self, series: str, title: str, brief: str, priority: int = 2, facts: str = "") -> dict[str, Any]:
        return {"idea_id": self.store.add_idea(series, title, brief, priority, facts)}

    def save_story_bank(self, facts: str) -> dict[str, Any]:
        """Owner's own verified figures/stories ("(est.)" = estimate). Used as FACTS for non-IPO posts and
        checked by the numbers gate, so a story post can only state numbers that appear here."""
        self.store.set_kv("story_bank", facts.strip())
        return {"saved": True, "chars": len(facts.strip())}

    def idea_facts(self, idea: Any) -> str:
        """Facts for an idea: its own, else (non-IPO only) the owner's story bank."""
        own = (idea["facts"] or "").strip()
        if own or idea["series"] == "ipo":
            return own
        return (self.store.get_kv("story_bank", "") or "").strip()

    def update_idea(self, idea_id: int, facts: str | None = None, status: str | None = None,
                    priority: int | None = None, brief: str | None = None) -> dict[str, Any]:
        if status and status not in {"backlog", "drafted", "published", "dropped"}:
            raise RuntimeError("status must be backlog, drafted, published or dropped")
        if self.store.get_idea(idea_id) is None:
            raise RuntimeError(f"No idea {idea_id}")
        fields = {k: v for k, v in (("facts", facts), ("status", status), ("priority", priority), ("brief", brief)) if v is not None}
        self.store.update_idea(idea_id, **fields)
        return {"idea_id": idea_id, "updated": sorted(fields)}

    def propose_post(self, idea_id: int | None = None, arm: tuple[str, str] | None = None,
                     arms: set[str] | None = None, hints: list[str] | None = None) -> dict[str, Any]:
        prof, ints = self.store.get_kv("profile"), self.store.get_kv("interests")
        if not prof or not ints:
            raise RuntimeError("Need both sync_profile and save_interests before propose_post")
        idea = None
        if idea_id is not None:
            idea = self.store.get_idea(idea_id)
            if idea is None:
                raise RuntimeError(f"No idea {idea_id}")
        rows, scored, _ = self._history_scores()
        bandit = StyleBandit.from_history(scored)
        posts = self.store.list_posts()
        recent_keys = {arm_key(p["style"], p["hook"]) for p in posts[-2:]}  # force variety
        style, hook = arm if arm else bandit.choose(exclude=recent_keys, include=arms)

        text_by_post = {p["id"]: p["text"] for p in posts}
        best = sorted(zip(rows, scored), key=lambda t: -t[1][2])[:3]
        top_texts = [text_by_post[r["post_id"]] for r, _ in best]
        recent_texts = [p["text"] for p in posts[-5:]]

        rules = feedback.rules_for_prompt(self.store.active_rules())  # lessons learned from real comments
        extra = {"hints": hints} if hints else {}
        if self.autopilot.format_today() == "short":  # today's cell in the format trial, or its decided format
            extra["short"] = True
        idea_d = dict(idea) if idea else None
        if idea_d is not None:
            idea_d["facts"] = self.idea_facts(idea)  # story bank fills non-IPO ideas that carry no facts of their own
        no_ph = idea is not None and (idea["series"] != "ipo" or not (idea["facts"] or "").strip())  # a [TBD] would only block the post
        banned = self.store.get_kv("banned_terms", []) or []
        if banned:
            extra["banned_terms"] = banned
        fh = formulas.formula_hint(style, len(posts))
        if fh:
            extra["formula_hint"] = fh
        vb = ownvoice.voice_block(self.store.get_kv("voice_fingerprint") or {})
        if vb:
            extra["voice_block"] = vb
        out = self.text.draft_post(prof, ints, style, hook, top_texts, recent_texts, idea_d, rules,
                                   no_placeholders=no_ph, **extra)
        draft_id = self.store.add_draft(style, hook, out["text"], out.get("image_prompt"))
        if idea:
            self.store.update_idea(idea["id"], status="drafted", draft_id=draft_id)
        return {
            "draft_id": draft_id, "idea_id": idea_id, "style": style, "hook": hook, "text": out["text"],
            "image_prompt": out.get("image_prompt"), "status": "pending_approval",
            "next": "Review the text, optionally edit_draft / generate_image, then approve_and_publish.",
        }

    def edit_draft(self, draft_id: int, text: str | None, image_prompt: str | None) -> dict[str, Any]:
        d = self._pending(draft_id)
        fields = {k: v for k, v in (("text", text), ("image_prompt", image_prompt)) if v}
        self.store.update_draft(d["id"], **fields)
        return {"draft_id": draft_id, "updated": sorted(fields)}

    def generate_image(self, draft_id: int) -> dict[str, Any]:
        d = self._pending(draft_id)
        if not d["image_prompt"]:
            raise RuntimeError("Draft has no image_prompt; use edit_draft to set one")
        path = self.image.generate(d["image_prompt"], f"draft-{draft_id}-{int(time.time())}")
        self.store.update_draft(draft_id, image_path=str(path))
        return {"draft_id": draft_id, "image_path": str(path)}

    def attach_image(self, draft_id: int, filename: str) -> dict[str, Any]:
        """Attach a user-supplied image from the images folder (no path traversal)."""
        d = self._pending(draft_id)
        path = (self.cfg.images_dir / filename).resolve()
        if path.parent != self.cfg.images_dir.resolve():
            raise RuntimeError("filename must be a plain file name inside the images folder")
        if not path.is_file():
            raise RuntimeError(f"'{filename}' not found in the images folder")
        if path.suffix.lower() not in IMAGE_TYPES:
            raise RuntimeError("Use a .png, .jpg or .jpeg image")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise RuntimeError("Image larger than 8 MB; compress it first")
        self.store.update_draft(d["id"], image_path=str(path))
        return {"draft_id": draft_id, "image_path": str(path)}

    # --- IPO filings (RHP) ---
    def ingest_rhp(self, filename: str) -> dict[str, Any]:
        """Extract key facts, with page numbers, from an RHP PDF in the filings folder."""
        from .ipo import rhp

        path = (self.cfg.filings_dir / filename).resolve()
        if path.parent != self.cfg.filings_dir.resolve():
            raise RuntimeError("filename must be a plain file name inside the filings folder")
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise RuntimeError(f"'{filename}' is not a PDF in the filings folder")
        pdf, pages, sections = rhp.build_excerpt(path)
        passes = max(1, self.cfg.extraction_passes)  # 1 on a free key; 2 adds a consensus check at double the requests
        for _ in range(passes):
            self.spend("extraction")
        client = self.gemini_client("extraction")
        data = self._extract(client, pdf, pages)
        disagreements = []
        if passes >= 2:
            second = self._extract(client, pdf, pages)
            disagreements = rhp.consensus_filter(data, second)  # keep only figures two runs agree on
        data["_excerpt_pages"] = sections
        report = rhp.verify_extraction(path, data)
        report["run_disagreements"] = disagreements
        data["_verification"] = report
        ipo_id = self.store.add_ipo(data.get("company") or filename, filename, data)
        if report["passed"]:
            self.store.update_ipo(ipo_id, verified=True)
        return {
            "ipo_id": ipo_id, "verified": report["passed"], "auto_check": report,
            "pages_sent": len(pages), "sections": sections,
            "document_type": data.get("document_type"), "facts": rhp.format_facts(data),
            "next": "Auto-verified." if report["passed"] else "Auto-check failed: see auto_check.missing / arithmetic_issues; fix with correct_ipo.",
        }

    def _extract(self, client: Any, pdf: bytes, pages: list[int]) -> dict[str, Any]:
        """Read a filing with the extraction model; if Google refuses or overloads it, retry once on the light fallback model
        (same key, counted as one more request). The verifier still checks every figure either way."""
        from google.genai import errors

        from .ipo import rhp

        models = [self.cfg.extraction_model]
        if self.cfg.gemini_extraction_fallback_model not in ("", models[0]):
            models.append(self.cfg.gemini_extraction_fallback_model)
        from .gemini.keys import count_model_use, model_at_daily_limit, rpm_guard, throttle

        kind = self.cfg.usage_kind("extraction")
        for i, m in enumerate(models):
            if i < len(models) - 1 and model_at_daily_limit(self.store, kind, m):
                self.store.add_log("model_fallback", f"extraction: {m} is at its daily limit; using {models[i + 1]}")
                continue  # ingest_rhp already counted this request once
            throttle(f"{kind}:{m}", rpm_guard(m))
            count_model_use(self.store, kind, m)
            try:
                return rhp.extract_facts(client, m, pdf, pages)
            except errors.APIError as e:
                if i == len(models) - 1 or e.code not in (429, 500, 503, 504):
                    raise
                self.spend("extraction")
                self.store.add_log("model_fallback", f"extraction: {m} refused ({e.code}); trying {models[i + 1]}")
        raise AssertionError("unreachable")

    def enrich_ipo(self, ipo_id: int, fetch_band: bool = True) -> dict[str, Any]:
        """Add sourced context to an IPO: the company's own business sentence, offer-for-sale size, and the price band
        from SEBI's addendum (one Gemini request, at most once a day until found). Returns the updated data."""
        import time as _t
        from .ipo import enrich

        row = self.store.get_ipo(ipo_id)
        if row is None:
            raise RuntimeError(f"No IPO {ipo_id}")
        data = json.loads(row["data"])
        pdf = self.cfg.filings_dir / row["source_file"]
        issue = data.setdefault("issue", {})
        if pdf.exists():
            if "business" not in data:
                data["business"] = enrich.business_sentence(pdf) or {}
            if not issue.get("ofs_shares"):
                issue.update(enrich.issue_structure(pdf))
        today = _t.strftime("%Y-%m-%d")
        if fetch_band and self.cfg.key_for("extraction") and not issue.get("price_band_high") and data.get("_band_tried") != today:
            data["_band_tried"] = today
            try:
                self.spend("extraction")
                band = enrich.fetch_price_band(self.gemini_client("extraction"), self.cfg.gemini_text_model, row["company"])
                if band:
                    issue.update(band)
            except Exception as e:
                self.store.add_log("enrich_error", f"{row['company']}: price band lookup failed: {e}"[:300], ipo_id=ipo_id)
        self.store.set_ipo_data(ipo_id, data)
        return data

    def get_ipo(self, ipo_id: int) -> dict[str, Any]:
        from .ipo import rhp

        row = self.store.get_ipo(ipo_id)
        if row is None:
            raise RuntimeError(f"No IPO {ipo_id}")
        data = json.loads(row["data"])
        return {"ipo_id": ipo_id, "company": row["company"], "verified": bool(row["verified"]),
                "facts": rhp.format_facts(data), "data": data}

    def correct_ipo(self, ipo_id: int, corrections: dict[str, Any]) -> dict[str, Any]:
        """Deep-merge corrections into the extracted data (dicts merge; lists and scalars replace). Resets verified."""
        row = self.store.get_ipo(ipo_id)
        if row is None:
            raise RuntimeError(f"No IPO {ipo_id}")

        def merge(a: dict, b: dict) -> dict:
            for k, v in b.items():
                a[k] = merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
            return a

        self.store.update_ipo(ipo_id, data=merge(json.loads(row["data"]), corrections))
        return self.get_ipo(ipo_id)

    def confirm_ipo(self, ipo_id: int) -> dict[str, Any]:
        """Mark the extracted figures as human-verified (required before they can feed a post)."""
        if self.store.get_ipo(ipo_id) is None:
            raise RuntimeError(f"No IPO {ipo_id}")
        self.store.update_ipo(ipo_id, verified=True)
        return {"ipo_id": ipo_id, "verified": True}

    def attach_ipo_facts(self, ipo_id: int, idea_id: int) -> dict[str, Any]:
        """Copy verified IPO facts into a backlog idea so drafts can cite them."""
        info = self.get_ipo(ipo_id)
        if not info["verified"]:
            raise RuntimeError("IPO figures are not verified yet; run confirm_ipo after checking them against the RHP")
        if self.store.get_idea(idea_id) is None:
            raise RuntimeError(f"No idea {idea_id}")
        self.store.update_idea(idea_id, facts=info["facts"])
        return {"idea_id": idea_id, "ipo_id": ipo_id, "facts_chars": len(info["facts"])}

    def review_draft(self, draft_id: int) -> dict[str, Any]:
        d = self._pending(draft_id)
        image = None
        if d["image_path"]:
            p = Path(d["image_path"])
            image = (p.read_bytes(), IMAGE_TYPES[p.suffix.lower()])
        return self.text.review(
            d["text"], d["image_prompt"], image, self.store.get_kv("profile"), self.store.get_kv("interests")
        )

    def _pending(self, draft_id: int):
        d = self.store.get_draft(draft_id)
        if d is None:
            raise RuntimeError(f"No draft {draft_id}")
        if d["status"] != "pending_approval":
            raise RuntimeError(f"Draft {draft_id} is '{d['status']}', not pending approval")
        return d

    # --- publishing (the approval gate) ---
    def approve_and_publish(self, draft_id: int, confirm: bool) -> dict[str, Any]:
        if confirm is not True:
            raise RuntimeError("Publishing requires confirm=true after the user has reviewed this exact draft")
        d = self._pending(draft_id)
        if "[TBD" in d["text"]:
            raise RuntimeError("Draft still contains [TBD ...] placeholders; fill in or remove them with edit_draft first")
        if self.store.posts_published_since(time.time() - WEEK) >= self.cfg.max_posts_per_week:
            raise RuntimeError(f"Weekly cap of {self.cfg.max_posts_per_week} posts reached")
        ipo_series = any(i["series"] == "ipo" and i["draft_id"] == draft_id for i in self.store.list_ideas())
        tags = None  # non-IPO posts: the picker decides, topped up with the model's own tags if it chose fewer than 4
        if not ipo_series:
            tags = pick_hashtags(d["text"], stats=self.store.tag_stats())
            seen = {t.lower() for t in tags}
            for own in re.findall(r"(?<!\w)#\w+", d["text"]):
                if len(tags) >= 4:
                    break
                if own.lower() not in seen:
                    tags.append(own)
                    seen.add(own.lower())
        urn, url = publish.publish_post(self.cfg, d["text"], d["image_path"], ipo_series, tags)
        post_id = self.store.add_post(draft_id, urn, url)
        if tags:
            self.store.add_post_tags(post_id, tags)
        return {"post_id": post_id, "urn": urn, "url": url}

    def schedule_publish(self, draft_id: int, at: str) -> dict[str, Any]:
        """Publish this exact draft at a local date-time (ISO, e.g. 2026-11-04T18:07). The server does it, no session needed."""
        from datetime import datetime

        ts = datetime.fromisoformat(at).timestamp()
        if ts < time.time() - 60:
            raise RuntimeError("That time is in the past")
        return self.autopilot.schedule_publish(draft_id, ts)

    def linkedin_auth_url(self) -> dict[str, str]:
        return {"url": oauth.auth_url(self.cfg), "then": "Sign in, copy the `code` query param from the redirect URL, call linkedin_auth_complete"}

    def linkedin_auth_complete(self, code: str) -> dict[str, Any]:
        return oauth.exchange_code(self.cfg, code)

    # --- metrics ---
    def due_checkpoints(self, now: float | None = None) -> list[tuple[int, str, str]]:
        """(post_id, checkpoint, urn) for checkpoints that have elapsed and have no stored snapshot."""
        now = now or time.time()
        due = []
        ignored = set(self.store.get_kv("ignored_posts", []))  # deleted on LinkedIn: nothing to scrape
        for p in self.store.list_posts():
            if p["id"] in ignored:
                continue
            have = {r["checkpoint"] for r in self.store.conn.execute(
                "SELECT checkpoint FROM metrics WHERE post_id=?", (p["id"],))}
            for name, hours in CHECKPOINTS:
                if name not in have and now >= p["published_at"] + hours * 3600:
                    due.append((p["id"], name, p["urn"]))
        return due

    async def refresh_metrics(self) -> dict[str, Any]:
        done, failed = [], []
        for post_id, cp, urn in self.due_checkpoints():
            try:
                m = await analytics.scrape_post_metrics(self.browser, urn)
                self.store.save_metrics(post_id, cp, **m)
                done.append({"post_id": post_id, "checkpoint": cp, **m})
            except Exception as e:  # keep going; report per post
                failed.append({"post_id": post_id, "checkpoint": cp, "error": str(e)})
        return {"saved": done, "failed": failed}

    async def analytics_now(self, post_id: int | None = None) -> dict[str, Any]:
        """Scrape current numbers for one post (default: the latest). Not stored, so a young post never skews the 24h/72h/7d scoring."""
        posts = self.store.list_posts()
        if post_id is not None:
            posts = [p for p in posts if p["id"] == post_id]
        elif posts:
            posts = [max(posts, key=lambda p: p["published_at"])]
        if not posts:
            return {"error": "no published post found"}
        p = posts[0]
        age_h = round((time.time() - p["published_at"]) / 3600, 1)
        try:
            m = await analytics.scrape_post_metrics(self.browser, p["urn"])
        except Exception as e:
            return {"post_id": p["id"], "url": p["url"], "age_hours": age_h, "error": str(e),
                    "hint": "If this says not logged in, run `--login` once; or enter numbers with record_metrics."}
        self.store.log_metrics(p["id"], "live", None, m["impressions"], m["reactions"], m["comments"], m["reposts"])
        return {"post_id": p["id"], "url": p["url"], "age_hours": age_h, "live": m}

    def session_state(self) -> dict[str, Any]:
        """Last known state of the saved LinkedIn browser login, as seen by the hourly snapshot (no page load)."""
        from datetime import datetime

        s = self.store.get_kv("browser_session", {})
        if not s:
            return {"ok": None, "note": "not checked yet"}
        return {**s, "checked": datetime.fromtimestamp(s["checked_at"]).strftime("%m-%d %H:%M")}

    async def snapshot_analytics(self, max_age_days: int = 14, max_posts: int = 10, force: bool = False) -> dict[str, Any]:
        """Hourly: read every young post's live analytics page and append the numbers (metrics_log, post_audience, CSV).

        Nothing is ever overwritten or deleted. Posts listed in kv 'ignored_posts' (deleted on LinkedIn) are skipped.
        Also records whether the saved login still works."""
        import csv
        from datetime import datetime

        now = time.time()
        ignored = set(self.store.get_kv("ignored_posts", []))
        posts = [p for p in self.store.list_posts() if p["id"] not in ignored and now - p["published_at"] <= max_age_days * 86400]
        posts = sorted(posts, key=lambda p: -p["published_at"])

        def due(p: Any) -> bool:
            """Young posts (under 72 h) are read every hour; older ones every 6 hours, so every post inside the window
            keeps being tracked without the page budget growing with each new post."""
            if force:
                return True
            if now - p["published_at"] < 72 * 3600:
                return True
            last = self.store.conn.execute("SELECT MAX(captured_at) FROM metrics_log WHERE post_id=?", (p["id"],)).fetchone()[0] or 0
            return now - last >= 6 * 3600

        posts = [p for p in posts if due(p)][:max_posts]
        snap = datetime.now().isoformat(timespec="minutes")
        out_csv = self.cfg.data_dir / "post_snapshots.csv"
        fresh = not out_csv.exists()
        done: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        expired = False
        with out_csv.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if fresh:
                w.writerow(["captured_at", "post_id", "age_hours", "impressions", "reactions", "comments", "reposts", "members_reached",
                            "in_network_pct", "profile_viewers", "followers_gained", "saves", "sends"])
            for p in posts:
                try:
                    r = await analytics.scrape_post_summary(self.browser, p["urn"])
                except LoginExpired as e:
                    failed.append({"post_id": p["id"], "error": str(e)[:160]})
                    expired = True
                    break  # every other page would fail the same way
                except Exception as e:  # a parse failure, a deleted post or a busy browser says nothing about the login
                    failed.append({"post_id": p["id"], "error": f"{type(e).__name__}: {str(e)[:160]}"})
                    continue
                m, x = r["metrics"], r["extra"]
                self.store.log_metrics(p["id"], "hourly", None, m["impressions"], m["reactions"], m["comments"], m["reposts"])
                self.store.save_audience(p["id"], snap, r["rows"])
                inn = next((pct for c, v, pct, _ in r["rows"] if v == "In-network share"), None)
                w.writerow([snap, p["id"], round((now - p["published_at"]) / 3600, 1), m["impressions"], m["reactions"], m["comments"],
                            m["reposts"], x["members_reached"], "" if inn is None else round(inn * 100, 1), x["profile_viewers"],
                            x["followers_gained"], x["saves"], x["sends"]])
                done.append({"post_id": p["id"], **m})
        if expired or done:  # a run that failed only on one post's parsing says nothing about the login
            self.store.set_kv("browser_session", {"ok": bool(done) and not expired, "checked_at": time.time()})
        return {"snapshot": snap, "saved": done, "failed": failed, "session_expired": expired}

    def record_metrics(self, post_id: int, checkpoint: str, impressions: int, reactions: int, comments: int, reposts: int) -> dict[str, Any]:
        self.store.save_metrics(post_id, checkpoint, impressions, reactions, comments, reposts)
        return {"saved": True}

    def import_metrics_csv(self, content: str) -> dict[str, Any]:
        """Match CSV rows to posts by post id in the URL; stored as the '7d' checkpoint."""
        return self._match_export_rows(analytics.parse_export_csv(content))

    def import_metrics_file(self, path: str, checkpoint: str = "7d", dry_run: bool = False) -> dict[str, Any]:
        """Import a LinkedIn export saved on disk (.xlsx or .csv). dry_run only reports what would be stored."""
        p = Path(path)
        if not p.is_file():
            raise RuntimeError(f"No such file: {path}")
        if p.suffix.lower() in (".xlsx", ".xlsm"):
            parsed = analytics.parse_export_xlsx(str(p))
        else:
            parsed = analytics.parse_export_csv(p.read_text(encoding="utf-8-sig"))
        return self._match_export_rows(parsed, checkpoint, dry_run)

    def _match_export_rows(self, parsed: list[dict[str, Any]], checkpoint: str = "7d", dry_run: bool = False) -> dict[str, Any]:
        if checkpoint not in {c for c, _ in CHECKPOINTS}:
            raise RuntimeError(f"checkpoint must be one of {[c for c, _ in CHECKPOINTS]}")
        posts = self.store.list_posts()
        found: list[dict[str, Any]] = []
        for row in parsed:
            for p in posts:
                pid = p["urn"].rsplit(":", 1)[-1]
                if pid and pid in row["url"]:
                    if not dry_run:
                        self.store.save_metrics(p["id"], checkpoint, row["impressions"], row["reactions"], row["comments"], row["reposts"])
                    found.append({"post_id": p["id"], "checkpoint": checkpoint,
                                  **{k: row[k] for k in ("impressions", "reactions", "comments", "reposts")}})
        return {"matched": len(found), "stored": not dry_run, "rows": found}

    def import_audience_file(self, path: str) -> dict[str, Any]:
        """Save the audience demographics from a single-post export and refresh data/audience_demographics.csv for analysis."""
        import csv
        from datetime import datetime

        p = Path(path)
        if not p.is_file():
            raise RuntimeError(f"No such file: {path}")
        parsed = analytics.parse_audience_xlsx(str(p))
        post = next((x for x in self.store.list_posts() if x["urn"].rsplit(":", 1)[-1] in parsed["url"]), None)
        if post is None:
            raise RuntimeError("This export is not for a post published by this system (post id not found in its URL)")
        rows = [(c, v, pct, None) for c, v, pct in parsed["demographics"]]
        rows += [("Summary", "Impressions", None, parsed["impressions"]), ("Summary", "Members reached", None, parsed["members_reached"])]
        self.store.save_audience(post["id"], datetime.now().isoformat(timespec="seconds"), rows)
        out = self.cfg.data_dir / "audience_demographics.csv"
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["post_id", "snapshot", "category", "value", "pct", "num"])
            for r in self.store.audience_rows():
                w.writerow([r["post_id"], r["snapshot"], r["category"], r["value"], r["pct"], r["num"]])
        return {"post_id": post["id"], "saved_rows": len(rows), "csv": str(out)}

    def audience_report(self, post_id: int | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in self.store.audience_rows(post_id):
            key = f"post {r['post_id']} @ {r['snapshot']}"
            val = r["num"] if r["category"] == "Summary" else (None if r["pct"] is None else f"{r['pct']:.0%}")
            out.setdefault(key, {}).setdefault(r["category"], {})[r["value"]] = val
        return out

    def export_ipo_metrics(self) -> dict[str, Any]:
        """Flat per-IPO metrics (issue, financials, ratios, peers, derived values, linked post analytics) as CSVs for dashboards/ML."""
        import csv

        from .ipo import metrics as im

        conn = self.store.conn
        ipo_rows = conn.execute("SELECT id, data, verified, created_at FROM ipos ORDER BY id").fetchall()
        post_by_ipo: dict[int, Any] = {}
        for r in conn.execute(
            "SELECT l.ipo_id, p.id post_id, p.published_at, d.style, d.hook FROM autopilot_log l "
            "JOIN posts p ON p.draft_id=l.draft_id JOIN drafts d ON d.id=p.draft_id "
            "WHERE l.ipo_id IS NOT NULL AND l.draft_id IS NOT NULL ORDER BY p.published_at"
        ):
            post_by_ipo[r["ipo_id"]] = r
        flat, fins = [], []
        for r in ipo_rows:
            data = json.loads(r["data"])
            self._fill_issue_from_market(data)
            row = im.ipo_row(r["id"], data, bool(r["verified"]), r["created_at"])
            im.apply_extras(row, self.store.latest_ipo_extra(r["id"]),
                            [x for x in self.store.ipo_investor_rows() if x["ipo_id"] == r["id"]])
            fins += im.financial_rows(r["id"], data.get("company"), data)
            pr = post_by_ipo.get(r["id"])
            post = dict.fromkeys(im.POST_COLS)
            if pr is not None:
                m = conn.execute("SELECT impressions,reactions,comments,reposts FROM metrics_log WHERE post_id=? "
                                 "ORDER BY captured_at DESC LIMIT 1", (pr["post_id"],)).fetchone()
                post.update(post_id=pr["post_id"], post_published_at=pr["published_at"], post_style=pr["style"], post_hook=pr["hook"])
                if m is not None:
                    post.update(post_impressions=m["impressions"], post_reactions=m["reactions"],
                                post_comments=m["comments"], post_reposts=m["reposts"])
            flat.append({**row, **post})
        out_dir = self.cfg.data_dir
        investors = [{k: x[k] for k in x.keys() if k not in ("id", "captured_at")} for x in self.store.ipo_investor_rows()]
        files = {}
        peers = []
        for r in ipo_rows:
            data = json.loads(r["data"])
            for p in data.get("peers") or []:
                peers.append({"ipo_id": r["id"], "company": data.get("company"), "peer": p.get("name"), "revenue_cr": p.get("revenue_cr"),
                              "pe": p.get("pe"), "roe_pct": p.get("roe_pct"), "eps": p.get("eps"), "source": "RHP", "page": p.get("page")})
            ex = self.store.latest_ipo_extra(r["id"]).get("peer_comparison_chittorgarh")
            for p in json.loads(ex["value_text"]) if ex else []:
                peers.append({"ipo_id": r["id"], "company": data.get("company"), "peer": p["name"], "pe": p["pe"], "eps": p["eps_diluted"],
                              "nav": p["nav"], "ronw_pct": p["ronw_pct"], "pbv": p["pbv"], "source": "chittorgarh.com (secondary)"})
        import re as _re

        ours = {_norm_company(json.loads(r["data"]).get("company", "")): r["id"] for r in ipo_rows}
        cg_rows = self.store.cg_ipo_rows()
        market = [{"cg_id": c["cg_id"], "company": c["company"], "dates": c["dates"], "url": c["url"],
                   "ipo_id": next((v for k, v in ours.items() if k and (k in _norm_company(c["company"]) or _norm_company(c["company"]) in k)), None),
                   "last_fetched": c["last_fetched"]} for c in cg_rows]
        names = {c["cg_id"]: c["company"] for c in cg_rows}
        mfacts = [{"cg_id": f["cg_id"], "company": names.get(f["cg_id"]), "field": f["field"], "value_num": f["value_num"],
                   "value_text": f["value_text"], "captured_at": f["captured_at"]}
                  for f in self.store.conn.execute("SELECT * FROM cg_facts ORDER BY cg_id, field, captured_at")]
        minv = [{"cg_id": r["cg_id"], "company": names.get(r["cg_id"]), "investor": r["investor"], "round_type": r["round_type"],
                 "shares": r["shares"], "price_per_share": r["price_per_share"], "amount_cr": r["amount_cr"], "source": r["source"]}
                for r in self.store.conn.execute("SELECT * FROM cg_investors ORDER BY cg_id, round_type, shares DESC")]
        listing = self.listing_outcomes()
        for name, rows in (("ipo_metrics.csv", flat), ("ipo_financials.csv", fins), ("ipo_investors.csv", investors),
                           ("ipo_peers.csv", peers), ("market_ipos.csv", market), ("market_facts.csv", mfacts),
                           ("market_investors.csv", minv), ("listing_outcomes.csv", listing),
                           ("exchange_listing.csv", [dict(r) for r in self.store.exchange_listing_rows()])):
            if not rows:
                continue
            path = out_dir / name
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
                w.writeheader()
                w.writerows(rows)
            files[name] = str(path)
        return {"ipos": len(flat), "financial_rows": len(fins), "files": files,
                "missing": {r["company"]: r["missing_fields"] for r in flat}}

    def weekly_digest(self) -> dict[str, Any]:
        """Plain-text/markdown summary of the last 7 days from the database (no Gemini). Saved to data/digest/."""
        from datetime import date, datetime

        c = self.store.conn
        now = time.time()
        lines = [f"# Weekly digest, {date.today().isoformat()}", ""]
        posts = c.execute(
            "SELECT p.id, p.published_at, p.url, d.style, d.hook, "
            "(SELECT impressions FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) imp, "
            "(SELECT reactions FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) rea, "
            "(SELECT comments FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) com, "
            "(SELECT reposts FROM metrics_log m WHERE m.post_id=p.id ORDER BY captured_at DESC LIMIT 1) rep "
            "FROM posts p JOIN drafts d ON d.id=p.draft_id ORDER BY p.published_at").fetchall()
        week = [p for p in posts if p["published_at"] >= now - 7 * 86400]
        lines += [f"## Posts ({len(week)} this week, {len(posts)} in total)"]
        for p in week:
            when = datetime.fromtimestamp(p["published_at"]).strftime("%a %d %b %H:%M")
            m = f"{p['imp']} impressions, {external_reactions(p['rea'])} reactions from others, {p['com']} comments, {p['rep']} reposts" if p["imp"] is not None else "no analytics yet"
            lines.append(f"- {when} [{p['style']}/{p['hook']}] {m} - {p['url']}")
        scored = [p for p in posts if p["imp"]]
        if len(scored) > 1:
            best = max(scored, key=lambda p: p["imp"])
            lines.append(f"- Best by impressions so far: post {best['id']} ({best['imp']}).")
        lines += ["", "## Audience (latest snapshot)"]
        snap = c.execute("SELECT MAX(snapshot) s FROM post_audience").fetchone()["s"]
        if snap:
            for cat in ("Location", "Seniority", "Industry", "Company size"):
                top = c.execute("SELECT value,pct FROM post_audience WHERE snapshot=? AND category=? ORDER BY pct DESC LIMIT 3", (snap, cat)).fetchall()
                if top:
                    lines.append(f"- {cat}: " + ", ".join(f"{r['value']} {r['pct']:.0%}" for r in top if r["pct"] is not None))
        else:
            lines.append("- none stored yet (import a single-post export with import_audience_file)")
        lines += ["", "## IPO pipeline"]
        for r in c.execute("SELECT id, data, verified FROM ipos ORDER BY id"):
            d = json.loads(r["data"])
            iss = d.get("issue") or {}
            lines.append(f"- {d.get('company')}: {iss.get('open_date')} to {iss.get('close_date')}, verified={bool(r['verified'])}")
        listed = self.listing_outcomes()
        if listed:
            lines += ["", "## Listing outcomes (all synced IPOs, last 10)"]
            for r in listed[-10:]:
                lines.append(f"- {r['company']}: open {r['listing_gain_open_pct']:+.1f}%, last trade "
                             f"{'n/a' if r['listing_gain_last_pct'] is None else format(r['listing_gain_last_pct'], '+.1f') + '%'} vs issue price")
        lines += ["", "## System (last 7 days)"]
        cnt = c.execute("SELECT action, COUNT(*) n FROM autopilot_log WHERE ts>=? GROUP BY action ORDER BY n DESC", (now - 7 * 86400,)).fetchall()
        lines.append("- log: " + ", ".join(f"{r['action']} {r['n']}" for r in cnt) if cnt else "- log: empty")
        lines.append("- Gemini calls today: " + "; ".join(
            f"{k['purpose']} ({k['account']}) {k.get('used_today', 0)} of {k['daily_cap']}"
            + ("" if k["configured"] else " [no key, paused]") for k in self.api_keys_report()["keys"]))
        rules = c.execute("SELECT COUNT(*) n FROM feedback_rules WHERE active=1").fetchone()["n"]
        lines.append(f"- active writing rules from audience feedback: {rules}")
        text = "\n".join(lines) + "\n"
        out_dir = self.cfg.data_dir / "digest"
        out_dir.mkdir(exist_ok=True)
        path = out_dir / f"digest-{date.today().isoformat()}.md"
        path.write_text(text, encoding="utf-8")
        self.store.add_log("weekly_digest", str(path))
        return {"file": str(path), "text": text}

    def weekly_listing_facts(self, today: Any = None, days: int = 7, max_n: int = 10) -> dict[str, Any]:
        """FACTS for the weekly scorecard post: IPOs that listed in the last `days` days, with issue price, listing open, last trade and
        the gains, plus 'Computed:' summary lines. Built only from stored exchange listing-day data."""
        from datetime import date, timedelta
        from statistics import median

        today = today or date.today()
        start = today - timedelta(days=days)
        rows = [r for r in self.listing_outcomes()
                if r.get("listing_date") and start <= date.fromisoformat(r["listing_date"]) <= today and r["listing_gain_open_pct"] is not None]
        rows.sort(key=lambda r: -r["listing_gain_open_pct"])
        rows = rows[:max_n]
        if not rows:
            return {"n": 0, "facts": "", "rows": []}
        f = [f"Week: IPOs that listed from {start.day} {start.strftime('%b')} to {today.day} {today.strftime('%b %Y')} "
             "(exchange listing-day data, NSE where available). Gains are measured against the issue price and are day-one figures only."]
        f.append("Note: investors pay the issue price, which is the upper end of the price band. The gains apply only to investors who were "
                 "allotted shares, and allotment to retail investors in an oversubscribed IPO is by lottery.")
        for r in rows:
            last = "" if r["listing_last_trade"] is None else (
                f"; closed at Rs {r['listing_last_trade']:g} ({r['listing_gain_last_pct']:+.1f}% vs issue price)")
            f.append(f"Listing: {r['company']}: issue price Rs {r['issue_price']:g}; opened at Rs {r['listing_open']:g} "
                     f"({r['listing_gain_open_pct']:+.1f}% vs issue price){last}.")
        opens = [r["listing_gain_open_pct"] for r in rows]
        lasts = [r["listing_gain_last_pct"] for r in rows if r["listing_gain_last_pct"] is not None]
        n = len(rows)
        f.append(f"Computed: {n} IPOs listed.")
        f.append(f"Computed: {sum(1 for g in opens if g > 0)} of {n} IPOs opened above the issue price.")
        f.append(f"Computed: average listing-open gain {sum(opens) / n:+.1f}%; median {median(opens):+.1f}%.")
        if lasts:
            f.append(f"Computed: {sum(1 for g in lasts if g > 0)} of {len(lasts)} IPOs closed above the issue price.")
            f.append(f"Computed: average gain at the close {sum(lasts) / len(lasts):+.1f}%.")
        best, worst = rows[0], rows[-1]
        f.append(f"Computed: best listing {best['company']} {best['listing_gain_open_pct']:+.1f}% at open; "
                 f"weakest {worst['company']} {worst['listing_gain_open_pct']:+.1f}% at open.")
        fell = [r for r in rows if r["listing_last_trade"] is not None and r["listing_gain_open_pct"] > 0 and r["listing_gain_last_pct"] < r["listing_gain_open_pct"]]
        if fell:
            f.append(f"Computed: {len(fell)} of {n} IPOs gave back part of their opening gain by the close.")
        return {"n": n, "facts": "\n".join(f), "rows": rows}

    def sync_exchange_listings(self, max_files: int = 40) -> dict[str, Any]:
        """For every IPO with a confirmed listing day, store its NSE and BSE listing-day prices from the exchanges' own files.
        Each file is downloaded once (cached in data/exchange). A day whose file is not published yet is retried on the next cycle."""
        from datetime import date as _date

        from .ipo import exchange as ex

        cache = self.cfg.data_dir / "exchange"
        out: dict[str, Any] = {"stored": [], "missing": [], "files": 0}
        cache_rows: dict[tuple[str, str], Any] = {}
        for o in self.listing_outcomes():
            if not o.get("listing_date"):
                continue
            day = _date.fromisoformat(o["listing_date"])
            have = {r["exchange"] for r in self.store.exchange_listing_rows(o["cg_id"])}
            for exch in ("NSE", "BSE"):
                if exch in have:
                    continue
                key = (exch, o["listing_date"])
                if key not in cache_rows:
                    if out["files"] >= max_files:
                        continue
                    fresh = not (cache / f"{exch}_{day.strftime('%Y%m%d')}.csv").exists()
                    try:
                        cache_rows[key] = ex.fetch_day(exch, day, cache)
                    except Exception as e:
                        cache_rows[key] = None
                        out["missing"].append(f"{exch} {o['listing_date']}: {str(e)[:80]}")
                    if fresh:
                        out["files"] += 1
                rows = cache_rows.get(key)
                if rows is None:
                    continue
                found = ex.find_listing(rows, o["company"], o["issue_price"] if exch == "NSE" else None)
                if found is None:
                    out["missing"].append(f"{o['company']} not found in {exch} file of {o['listing_date']}")
                    continue
                self.store.add_exchange_listing(o["cg_id"], exch, o["listing_date"], found, f"{exch} bhavcopy {o['listing_date']}")
                out["stored"].append(f"{o['company']} {exch}")
        self.store.add_log("exchange_sync", f"{len(out['stored'])} stored, {len(out['missing'])} missing, {out['files']} files")
        return out

    def listing_outcomes(self) -> list[dict[str, Any]]:
        """One row per listed IPO: issue price, listing-day open/high/low/last trade on NSE (BSE if NSE is missing) and the
        gains versus the issue price. This is the outcome variable for later modelling."""
        out = []
        for c in self.store.cg_ipo_rows():
            f = {r["field"]: r["value_num"] for r in self.store.conn.execute(
                "SELECT field,value_num FROM cg_facts WHERE id IN (SELECT MAX(id) FROM cg_facts WHERE cg_id=? AND field LIKE 'Listing Day Trading Information|%' GROUP BY field)",
                (c["cg_id"],))}

            def get(label: str) -> float | None:
                return f.get(f"Listing Day Trading Information|{label}|NSE") or f.get(f"Listing Day Trading Information|{label}|BSE")

            issue, op, hi, lo, last = get("Final Issue Price"), get("Open"), get("High"), get("Low"), get("Last Trade")
            src, check = "chittorgarh (secondary)", None
            ex = {r["exchange"]: r for r in self.store.exchange_listing_rows(c["cg_id"])}
            prim = ex.get("NSE") or ex.get("BSE")
            if prim is not None and prim["open"]:
                if op:  # how far the secondary source is from the exchange's own open, as a quality check
                    check = round((op / prim["open"] - 1) * 100, 3)
                issue = ex["NSE"]["prev_close"] if ex.get("NSE") and ex["NSE"]["prev_close"] else issue
                op, hi, lo, last = prim["open"], prim["high"], prim["low"], prim["close"]
                src = f"{prim['exchange']} official file"
            if not issue or not op:
                continue  # not listed yet (or not shown yet)
            pct = lambda v: None if v is None else round((v / issue - 1) * 100, 2)  # noqa: E731
            # The listing day is the day we FIRST saw the exchange's listing-day trading data, and only if we had already seen the
            # IPO before it listed (its page still carried a 'Listing Date' row). An IPO found after it listed gets no date: shares do
            # not always list exactly 3 working days after close (holidays, market closures, schedule changes), so nothing is estimated.
            from datetime import datetime as _dt
            first = self.store.conn.execute(
                "SELECT MIN(captured_at) t FROM cg_facts WHERE cg_id=? AND field LIKE 'Listing Day Trading Information|%'", (c["cg_id"],)).fetchone()["t"]
            pre = self.store.conn.execute(
                "SELECT MIN(captured_at) t FROM cg_facts WHERE cg_id=? AND field='IPO Details|Listing Date'", (c["cg_id"],)).fetchone()["t"]
            listed_on = _dt.fromtimestamp(first).date().isoformat() if (first and pre and pre <= first) else None
            source = "first seen" if listed_on else None
            # Best source: the exchange-confirmed 'Listed on' row the page shows once an IPO has listed.
            import re as _re
            lo_row = self.store.conn.execute(
                "SELECT value_text FROM cg_facts WHERE cg_id=? AND field='IPO Details|Listed on' ORDER BY captured_at DESC, id DESC LIMIT 1",
                (c["cg_id"],)).fetchone()
            lm = _re.search(r"([A-Za-z]{3})[a-z]* (\d{1,2}), (\d{4})", lo_row["value_text"] if lo_row else "")
            if lm:
                try:
                    listed_on = _dt.strptime(f"{lm.group(1)} {lm.group(2)} {lm.group(3)}", "%b %d %Y").date().isoformat()
                    source = "listed on (confirmed)"
                except ValueError:
                    pass
            out.append({"cg_id": c["cg_id"], "company": c["company"], "listing_date": listed_on, "listing_date_source": source if listed_on else None, "price_source": src, "secondary_open_vs_exchange_pct": check,
                        "issue_price": issue, "listing_open": op,
                        "listing_high": hi, "listing_low": lo, "listing_last_trade": last,
                        "listing_gain_open_pct": pct(op), "listing_gain_last_pct": pct(last),
                        "listing_gain_high_pct": pct(hi), "listing_gain_low_pct": pct(lo),
                        "day_range_pct": None if hi is None or lo is None else round((hi - lo) / issue * 100, 2)})
        return out

    def market_fact_lines(self, company: str, ipo_id: int | None = None, price_band_high: float | None = None) -> list[str]:
        """FACTS lines for a post, built only from market data that is actually present (lot, objects, anchor round, sellers).
        Anything missing is simply not mentioned."""
        import re

        want = _norm_company(company)
        cg = next((c for c in self.store.cg_ipo_rows() if want and (want in _norm_company(c["company"]) or _norm_company(c["company"]) in want)), None)
        out: list[str] = []
        if ipo_id is not None:  # facts read from the RHP itself (promoter holding, pre-IPO round)
            prom = self.store.latest_ipo_extra(ipo_id).get("promoter_holding_pre_pct")
            if prom is not None and prom["value_num"]:
                out.append(f"Promoters hold {prom['value_num']:g}% of the company before the issue (RHP page {prom['page']}).")
            pre = [i for i in self.store.ipo_investor_rows() if i["ipo_id"] == ipo_id and i["round_type"] in ("pre_ipo", "private_placement")]
            prices = {i["price_per_share"] for i in pre if i["price_per_share"]}
            if pre and len(prices) == 1:
                p = prices.pop()
                out.append(f"Pre-IPO round ({pre[0]['allotment_date']}): {len(pre)} investors bought {sum(i['shares'] or 0 for i in pre):,} shares at Rs {p:g} each, "
                           f"about Rs {sum(i['shares'] or 0 for i in pre) * p / 1e7:.1f} crore in total.")
                if price_band_high:
                    rel = "above" if p > price_band_high else "below" if p < price_band_high else "equal to"
                    out.append(f"Computed: the pre-IPO price of Rs {p:g} is {rel} the IPO upper price band of Rs {price_band_high:g}.")
        if cg is None:
            return out
        facts: dict[str, Any] = {}
        for r in self.store.conn.execute(
            "SELECT field,value_text,value_num FROM cg_facts WHERE id IN (SELECT MAX(id) FROM cg_facts WHERE cg_id=? GROUP BY field)",
                (cg["cg_id"],)):
            facts[r["field"]] = r

        lot = facts.get("IPO Lot Size|Retail (Min)|Shares")
        amt = facts.get("IPO Lot Size|Retail (Min)|Amount")
        if lot is not None and lot["value_num"] and amt is not None and amt["value_num"]:
            out.append(f"Lot: {int(lot['value_num'])} shares; the minimum retail bid is Rs {int(amt['value_num']):,} at the upper price band.")

        objs = {}
        for f, r in facts.items():
            m = re.match(r"^IPO Objects of the Issue\|(\d+)\|(Issue Objects|Est Amt.*)$", f)
            if m:
                objs.setdefault(int(m.group(1)), {})["text" if m.group(2) == "Issue Objects" else "amt"] = r
        for n in sorted(objs):
            o = objs[n]
            if "text" in o:
                amt_s = f": Rs {o['amt']['value_num']:g} crore" if o.get("amt") and o["amt"]["value_num"] else ""
                out.append(f"Object: {o['text']['value_text'][:260]}{amt_s}")

        anchors: dict[int, dict[str, Any]] = {}
        for f, r in facts.items():
            m = re.match(r"^Anchor Investor Allocation\|(\d+)\|(Anchor|Amt.*)$", f)
            if m:
                anchors.setdefault(int(m.group(1)), {})["name" if m.group(2) == "Anchor" else "amt"] = r
        rows = [a for a in anchors.values() if a.get("name") and a.get("amt") and a["amt"]["value_num"]]
        if rows:
            total = sum(a["amt"]["value_num"] for a in rows)
            official = next((r["value_num"] for f, r in facts.items() if f.endswith("|Anchor Portion") and r["value_num"]), None)
            if official and abs(official - total) < 1:
                total = official
            top = sorted(rows, key=lambda a: -a["amt"]["value_num"])[:3]
            names = "; ".join(f"{a['name']['value_text']} Rs {a['amt']['value_num']:g} crore" for a in top)
            out.append(f"Anchor round: Rs {total:.2f} crore from {len(rows)} anchor investors. Largest: {names}.")
            cat = {m.group(1): r["value_num"] for f, r in facts.items()
                   for m in [re.match(r"^Anchor Investor Category-wise Allotment\|([A-Za-z ]+)\|Allotment %", f)] if m and r["value_num"] and not m.group(1).lower().startswith("total")}
            if cat:
                out.append("Anchor mix by category: " + ", ".join(f"{k} {v:g}%" for k, v in sorted(cat.items(), key=lambda kv: -kv[1])) + ".")

        sellers = self.store.conn.execute(
            "SELECT round_type, COUNT(*) n, SUM(amount_cr) cr FROM cg_investors WHERE cg_id=? AND round_type LIKE 'ofs_seller%' GROUP BY round_type",
            (cg["cg_id"],)).fetchall()
        if sellers:
            parts = [f"{'promoters' if 'promoter' in s['round_type'] else 'other shareholders'} ({s['n']}) sell Rs {s['cr']:.0f} crore" for s in sellers if s["cr"]]
            if parts:
                out.append("Offer for sale sellers: " + "; ".join(parts) + ". That money goes to the sellers, not the company.")
        return out

    def _fill_issue_from_market(self, data: dict[str, Any]) -> None:
        """Blank price band / lot size in the RHP extraction are filled from the latest chittorgarh facts (export only)."""
        import re

        want = _norm_company(data.get("company", ""))
        cg = next((c for c in self.store.cg_ipo_rows() if want and (want in _norm_company(c["company"]) or _norm_company(c["company"]) in want)), None)
        if cg is None:
            return
        issue = data.setdefault("issue", {})

        def latest(field: str) -> str | None:
            r = self.store.conn.execute("SELECT value_text FROM cg_facts WHERE cg_id=? AND field=? ORDER BY captured_at DESC, id DESC LIMIT 1",
                                        (cg["cg_id"], field)).fetchone()
            return r["value_text"] if r else None

        pb = re.search(r"([\d.]+)\D+to\D+([\d.]+)", (latest("IPO Details|Price Band") or "").replace(",", ""))
        if pb and issue.get("price_band_high") is None:
            issue["price_band_low"], issue["price_band_high"] = float(pb.group(1)), float(pb.group(2))
        lot = re.search(r"(\d[\d,]*)", latest("IPO Details|Lot Size") or "")
        if lot and issue.get("lot_size") is None:
            issue["lot_size"] = int(lot.group(1).replace(",", ""))

    def sync_market(self, max_pages: int = 40) -> dict[str, Any]:
        """Read chittorgarh.com: the IPO dashboard, then each IPO's detail, reviews/peers and subscription pages and its
        anchor letter. Plain HTTP, no Gemini. Polite: paced requests, a page budget, and refetch only when due."""
        import random
        from datetime import date, timedelta

        from .ipo import chittorgarh as cg

        today, pages, report = date.today(), 0, {"facts_added": 0, "anchors": [], "errors": []}

        def fetch(url: str) -> str:
            nonlocal pages
            if pages >= max_pages:
                raise RuntimeError("page budget reached")
            if pages:
                time.sleep(random.uniform(1.5, 3.0))
            pages += 1
            return cg.get(url).text

        for d in cg.parse_dashboard(fetch(cg.DASHBOARD)):
            self.store.upsert_cg_ipo(d["cg_id"], d["slug"], d["name"], d["url"], d["dates"])
        report["ipos_on_dashboard"] = len(self.store.cg_ipo_rows())
        src = "chittorgarh.com (secondary source)"
        now = time.time()
        # Only IPOs we write content about are read in depth; the rest of the dashboard stays a name list.
        tracked = [_norm_company(json.loads(r["data"]).get("company", "")) for r in self.store.conn.execute("SELECT data FROM ipos")]
        report["tracked"] = []
        for row in self.store.cg_ipo_rows():
            want = _norm_company(row["company"])
            if not any(t and (t in want or want in t) for t in tracked):
                # Not an IPO we write about. Its listing-day result still feeds the weekly scorecard, so read its detail page
                # (one request) after it closes, until the listing data shows up. Nothing else is fetched.
                win = cg.issue_window(row["dates"] or "", today)
                has_listing = self.store.conn.execute(
                    "SELECT 1 FROM cg_facts WHERE cg_id=? AND field LIKE 'Listing Day Trading Information|%' LIMIT 1", (row["cg_id"],)).fetchone()
                newest = self.store.conn.execute(
                    "SELECT MAX(captured_at) t FROM cg_facts WHERE cg_id=? AND field LIKE 'Listing Day Trading Information|%'", (row["cg_id"],)).fetchone()["t"]
                still_moving = bool(has_listing) and newest is not None and now - newest < 36 * 3600  # 'Last Trade' is final only after the close
                if (win and win[1] < today <= win[1] + timedelta(days=14) and (not has_listing or still_moving)
                        and now - (row["last_fetched"] or 0) >= 3 * 3600):
                    try:
                        report["facts_added"] += self.store.add_cg_facts(row["cg_id"], cg.parse_tables(fetch(row["url"])))
                        self.store.touch_cg_ipo(row["cg_id"])
                        report.setdefault("listing_refreshed", []).append(row["company"])
                    except Exception as e:
                        report["errors"].append(f"{row['company']}: {str(e)[:120]}")
                        if "budget" in str(e):
                            break
                continue
            report["tracked"].append(row["company"])
            win = cg.issue_window(row["dates"] or "", today)
            active = bool(win) and win[0] - timedelta(days=3) <= today <= win[1] + timedelta(days=3)
            recent = bool(win) and today <= win[1] + timedelta(days=14)
            age = now - (row["last_fetched"] or 0)
            sub_age = now - (row["last_subscription"] or 0)
            closed = bool(win) and win[1] < today
            lst = self.store.conn.execute(
                "SELECT MAX(captured_at) t FROM cg_facts WHERE cg_id=? AND field LIKE 'Listing Day Trading Information|%'", (row["cg_id"],)).fetchone()["t"]
            awaiting_listing = closed and recent and (lst is None or now - lst < 36 * 3600)
            due_main = (row["last_fetched"] is None or (active and age >= 3 * 3600) or (awaiting_listing and age >= 3 * 3600)
                        or (recent and age >= 24 * 3600))
            due_sub = active and sub_age >= 3600
            if not (due_main or due_sub):
                continue
            try:
                if due_main:
                    html = fetch(row["url"])
                    facts = cg.parse_tables(html)
                    report["facts_added"] += self.store.add_cg_facts(row["cg_id"], facts)
                    for s in cg.selling_shareholders(facts):
                        kind = "ofs_seller_promoter" if "promoter" in (s.get("category") or "").lower() else "ofs_seller_other"
                        self.store.add_cg_investor(row["cg_id"], s["name"], kind, s["shares"], None, s.get("amount_cr"), src)
                    letter = cg.anchor_letter_url(html)
                    if letter and not self.store.has_cg_investors(row["cg_id"], "anchor_"):
                        import httpx

                        res = cg.parse_anchor_letter(httpx.get(letter, headers=cg.UA, timeout=60, follow_redirects=True).content)
                        if res["ok"]:
                            for a in res["rows"]:
                                self.store.add_cg_investor(row["cg_id"], a["name"], "anchor_" + a["category"].lower().replace(" ", "_"),
                                                           a["shares"], a["price"], round(a["amount"] / 1e7, 4),
                                                           "company letter to BSE/NSE via " + src)
                            report["anchors"].append(row["company"])
                        else:
                            self.store.add_log("market_anchor_unparsed", f"{row['company']}: {res['reason']}")
                    peers = fetch(row["url"].replace("/ipo/", "/ipo-recommendation/", 1))
                    report["facts_added"] += self.store.add_cg_facts(row["cg_id"], cg.parse_tables(peers))
                    self.store.touch_cg_ipo(row["cg_id"])
                if due_sub or (due_main and active):
                    subs = fetch(row["url"].replace("/ipo/", "/ipo_subscription/", 1))
                    report["facts_added"] += self.store.add_cg_facts(row["cg_id"], cg.parse_tables(subs))
                    self.store.touch_cg_ipo(row["cg_id"], subscription=True)
            except Exception as e:  # one bad IPO must not stop the rest
                report["errors"].append(f"{row['company']}: {str(e)[:120]}")
                if "budget" in str(e):
                    break
        report["pages"] = pages
        try:
            report["exchange"] = self.sync_exchange_listings()
        except Exception as e:
            report["errors"].append(f"exchange files: {str(e)[:100]}")
        self.store.add_log("market_sync", f"{len(report['tracked'])} tracked IPOs, {report['facts_added']} new facts, {pages} pages, {len(report['errors'])} errors")
        return report

    # --- audience comments -> lasting writing rules ---
    async def analyze_comments(self, post_id: int, checkpoint: str) -> dict[str, Any]:
        """Read the comments on one of our posts, extract issues/praise, and store the lessons as rules."""
        row = self.store.conn.execute("SELECT url FROM posts WHERE id=?", (post_id,)).fetchone()
        if row is None or not row["url"]:
            raise RuntimeError(f"No URL stored for post {post_id}")
        page = await self.browser.read_text(row["url"], selector="main", scroll=6)
        self.spend("insights")
        existing = self.store.active_rules(25)
        result = feedback.analyze_comments(self.gemini_client("insights"), self.cfg.gemini_text_model, page, existing)
        known = {r["id"] for r in existing}
        added, reinforced = [], []
        for r in result["rules"]:
            if r["existing_rule_id"] is not None:
                if r["existing_rule_id"] in known:
                    self.store.reinforce_rule(r["existing_rule_id"])
                    reinforced.append(r["existing_rule_id"])
            else:
                added.append(self.store.upsert_rule(r["kind"], r["rule"], post_id))
        summary = {k: result.get(k) for k in ("comment_count", "sentiment", "issues", "praised", "questions")}
        summary["rules_added"], summary["rules_reinforced"] = added, reinforced
        self.store.save_post_feedback(post_id, checkpoint, summary)
        issues = "; ".join(f"{i['type']}: {i['detail']}" for i in result["issues"][:3]) or "none"
        self.store.add_log("feedback", f"post {post_id} @{checkpoint}: {summary['comment_count']} comments, "
                           f"sentiment {summary['sentiment']}, issues: {issues}; +{len(added)} new rules, "
                           f"{len(reinforced)} reinforced")
        if any(i["type"] == "factual_error" for i in result["issues"]):
            self.store.add_log("feedback_alert", f"post {post_id}: a commenter reports a factual error - check the post")
        return summary

    # --- followers and comment replies ---
    async def track_followers(self) -> dict[str, Any]:
        """Store today's follower count (one browser page). Returns the count and the change since the last reading."""
        from datetime import date

        from .linkedin import followers as fl

        n = await fl.scrape_followers(self.browser)
        if n is None:
            raise RuntimeError("Could not find a follower count on the profile page")
        prev = self.store.follower_rows(2)
        self.store.save_followers(date.today().isoformat(), n)
        before = next((r["count"] for r in prev if r["day"] != date.today().isoformat()), None)
        return {"followers": n, "change": None if before is None else n - before}

    def follower_history(self, limit: int = 90) -> dict[str, Any]:
        rows = [{"day": r["day"], "followers": r["count"]} for r in self.store.follower_rows(limit)]
        out: dict[str, Any] = {"days": rows}
        if len(rows) >= 2:
            out["change_since_first"] = rows[0]["followers"] - rows[-1]["followers"]
            out["span"] = f"{rows[-1]['day']} to {rows[0]['day']}"
        return out

    async def check_comments(self, post_id: int, force: bool = False) -> dict[str, Any]:
        """Read one post page and score the conversation: the comments received and the owner's own replies.

        No reply is ever drafted or sent. Gemini is called only when the page has changed since the last reading.
        """
        from .strategy import conversation as cv

        row = self.store.conn.execute("SELECT url FROM posts WHERE id=?", (post_id,)).fetchone()
        if row is None or not row["url"]:
            raise RuntimeError(f"No URL stored for post {post_id}")
        page = await self.browser.read_text(row["url"], selector="main", scroll=6)
        count = cv.comment_count(page)
        last = self.store.latest_conversation(post_id)
        if not force and last is not None and count <= last["comments"] and len(page) == self.store.get_kv(f"page_len_{post_id}", -1):
            return {"post_id": post_id, "comments": count, "analysed": False, "why": "nothing new since the last check"}
        self.spend("insights")
        a = cv.analyze_conversation(self.gemini_client("insights"), self.cfg.gemini_text_model, page)
        self.store.set_kv(f"page_len_{post_id}", len(page))
        self.store.add_conversation(post_id, a)
        return {"post_id": post_id, "analysed": True, **a}

    def conversation_report(self, limit: int = 50) -> dict[str, Any]:
        """Per-post conversation quality, newest first, plus the running averages and the latest coaching points."""
        rows = self.store.conversation_rows(limit)
        posts = []
        for r in rows:
            d = json.loads(r["detail"])
            posts.append({
                "post_id": r["post_id"], "comments": r["comments"], "my_replies": r["author_replies"],
                "reply_rate": None if r["reply_rate"] is None else round(r["reply_rate"], 2),
                "comment_quality": r["comment_quality"], "my_reply_quality": r["reply_quality"],
                "comment_types": d.get("comment_types"), "strongest_comment": d.get("strongest_comment"),
            })
        def avg(key: str) -> float | None:
            vals = [p[key] for p in posts if p.get(key) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None
        latest = json.loads(rows[0]["detail"]) if rows else {}
        return {
            "posts_measured": len(posts),
            "average_comment_quality": avg("comment_quality"),
            "average_reply_quality": avg("my_reply_quality"),
            "average_reply_rate": avg("reply_rate"),
            "coaching": latest.get("coaching", []),
            "weakest_own_reply": latest.get("weakest_own_reply", ""),
            "by_post": posts,
        }

    def reply_queue(self) -> dict[str, Any]:
        """Which posts need a reply now. Stage follows post age: <6h watch, 6-24h hot if unanswered, 24-72h cool,
        >72h move the talk to DMs. Replying inside ~90 minutes is the target. Read-only, no model call."""
        now, out = time.time(), []
        for p in self.store.list_posts():
            c = self.store.latest_conversation(p["id"]) if p["url"] else None
            if c is None:
                continue
            age = (now - p["published_at"]) / 3600
            if age > 120:
                continue
            unanswered = max(c["comments"] - c["author_replies"], 0)
            if age < 6:
                stage = "watch"
            elif age < 24:
                stage = "hot" if unanswered else "warm"
            elif age < 72:
                stage = "cool"
            else:
                stage = "move to DM"
            urgent = unanswered > 0 and age < 24
            out.append({"post_id": p["id"], "age_hours": round(age, 1), "stage": stage, "comments": c["comments"],
                        "unanswered": unanswered, "reply_now": urgent,
                        "advice": ("Reply within 90 minutes: add one new fact or ask one sharper question, no 'thanks!' alone."
                                   if urgent else "No unanswered comments." if not unanswered else "Reply only if it adds something; consider a DM.")})
        out.sort(key=lambda r: (not r["reply_now"], r["age_hours"]))
        return {"queue": out, "reply_now": sum(1 for r in out if r["reply_now"])}

    def feedback_report(self) -> dict[str, Any]:
        return {
            "active_rules": [{"id": r["id"], "kind": r["kind"], "rule": r["rule"], "hits": r["hits"]}
                             for r in self.store.active_rules(25)],
            "recent_feedback": [{"post_id": r["post_id"], "checkpoint": r["checkpoint"], **json.loads(r["summary"])}
                                for r in self.store.recent_feedback(8)],
        }

    # --- analysis ---
    def analyze_performance(self) -> dict[str, Any]:
        raw, scored, baseline = self._history_scores()
        rows = [dict(r) for r in raw]
        for r, (_, _, s) in zip(rows, scored):
            r["score"] = round(s, 3)
        if not rows:
            return {"posts": 0, "note": "No metrics yet. Publish a post and run refresh_metrics after 24h."}
        return {"posts": len(rows), "baseline_impressions": round(baseline), "rows": rows,
                "summary": self.insights_text.analyze(rows)}

    # --- hooks and format experiments (analysis only; the autopilot's format is never changed by these) ---
    def check_hook(self, text: str) -> dict[str, Any]:
        """Lint a draft's opening against the rule book. No Gemini call."""
        from .strategy.hooks import lint_hook

        return lint_hook(text)

    def hook_report(self) -> dict[str, Any]:
        """Which opening patterns earned reach on your published posts, with honest sample-size caveats. No Gemini call."""
        from .strategy.hooks import hook_report, lint_hook

        rows = [dict(r) for r in self.store.post_texts()]
        out = hook_report(rows)
        out["posts_detail"] = [{"post_id": r["post_id"], "impressions": r["impressions"], "comments": r["comments"],
                                "hook_score": lint_hook(r["text"])["score"], "hook": lint_hook(r["text"])["hook"][:120]}
                               for r in rows]
        return out

    def suggest_hooks(self, text: str, topic: str = "") -> dict[str, Any]:
        """Alternative opening lines for a draft, each linted. Costs one Gemini call (counts toward the daily cap)."""
        from .strategy.hooks import lint_hook

        raw = self.text.suggest_hooks(text, topic).get("hooks", [])
        return {"current": lint_hook(text),
                "alternatives": [{**h, "lint": lint_hook(h.get("line", ""))} for h in raw if h.get("line")]}

    def experiment_status(self, today: Any = None) -> dict[str, Any]:
        """The concurrent format + posting-time trial: locked, running or decided. Never changes the live format itself."""
        from datetime import date, datetime

        from .strategy import experiments as ex

        state = self.store.get_kv("experiments", None) or ex.new_state()
        today = today or date.today()
        trial = state["trial"]
        out: dict[str, Any] = {"format_locked_until": ex.START_AFTER.isoformat(), "can_start": ex.can_start(today),
                               "what_is_tested": ex.CATALOG, "history": state["history"], "trial": trial}
        if trial:
            kinds = self.store.post_kinds()
            posts = [{"post_id": r["post_id"], "kind": kinds.get(r["post_id"], "pillar"), "impressions": r["impressions"],
                      "comments": r["comments"], "day": datetime.fromtimestamp(r["published_at"]).date().isoformat()}
                     for r in self.store.post_metrics_72h() if r["post_id"] >= trial["first_post_id"]]
            out["evaluation"] = ex.analyze(trial, posts)
        return out

    def start_experiment(self, name: str = "trial", today: Any = None) -> dict[str, Any]:
        """Start the trial (the autopilot normally does this itself on 2026-11-04). Refused before that date."""
        from datetime import date

        from .strategy import experiments as ex

        state = self.store.get_kv("experiments", None) or ex.new_state()
        newest = self.store.conn.execute("SELECT COALESCE(MAX(id), 0) m FROM posts").fetchone()["m"]
        today = today or date.today()
        ex.start_trial(state, today, newest + 1)
        self.store.set_kv("experiments", state)
        return self.experiment_status(today)

    def finish_experiment(self, today: Any = None) -> dict[str, Any]:
        """Archive the trial once both the format and the posting slot are decided. Results are kept, never deleted."""
        from datetime import date

        from .strategy import experiments as ex

        state = self.store.get_kv("experiments", None) or ex.new_state()
        if not state["trial"]:
            return {"note": "No trial is running."}
        if not ex.finish(state, today or date.today()):
            return {"note": "Not finished: both the format and the slot need a final verdict.",
                    "evaluation": self.experiment_status(today)["evaluation"]}
        self.store.set_kv("experiments", state)
        return {"archived": state["history"][-1]}

    def next_strategy(self) -> dict[str, Any]:
        _, scored, _ = self._history_scores()
        b = StyleBandit.from_history(scored)
        return {
            "posts_scored": len(scored),
            "top_arms": [{"arm": k, "expected_score": round(m, 3), "samples": n} for k, m, n in b.ranking()[:5]],
            "note": "propose_post samples from these posteriors; with <10 posts, expect mostly exploration.",
        }
