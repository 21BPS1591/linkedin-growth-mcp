"""Gemini text: profile-fit assessment and post drafting."""
from __future__ import annotations

import json
import time
from typing import Any

from google import genai
from google.genai import errors, types

from ..config import Config
from ..store.db import Store
from ..strategy.styles import HOOKS, STYLES
from .keys import KeyMissing, count_model_use, model_at_daily_limit, rpm_guard, throttle
from .retry import with_retry


class GeminiText:
    def __init__(self, cfg: Config, store: Store, purpose: str = "content"):
        if not cfg.key_for(purpose):
            raise KeyMissing(purpose)
        self.cfg = cfg
        self.store = store
        self.purpose = purpose
        self.client = genai.Client(api_key=cfg.key_for(purpose), http_options=types.HttpOptions(timeout=180_000))
        self._backup_client = None

    def _backup(self):
        """The client of the key that backs up content work, or None. Only the content job has a backup."""
        if self.purpose != "content" or not self.cfg.content_backup_key():
            return None
        if self._backup_client is None:
            self._backup_client = genai.Client(api_key=self.cfg.content_backup_key(), http_options=types.HttpOptions(timeout=180_000))
        return self._backup_client

    def _json_models(self) -> tuple[str, str]:
        """(model, fallback model) for JSON drafting. The writer gets the stronger model; other jobs use the light text model."""
        if self.purpose == "content":
            return self.cfg.draft_model, self.cfg.gemini_draft_fallback_model
        return self.cfg.gemini_text_model, ""

    def _generate(self, call, model: str, fallback: str = ""):
        """Run one request on this job's own key, counted against its daily cap.

        If `model` is refused (429) or overloaded (5xx after gentle retries), the lighter `fallback` model is tried on the same
        key. Content work only: if that key's cap is reached, or Google says its quota is spent, the request goes to the backup
        key instead (counted there). No other job ever fails over, and a 429 is never retried on the same key and model."""
        models = [model] + ([fallback] if fallback and fallback != model else [])
        kind, cap = self.cfg.usage_kind(self.purpose), self.cfg.cap_for(self.purpose)
        last = None
        for i, m in enumerate(models):
            if i < len(models) - 1 and model_at_daily_limit(self.store, kind, m):
                continue  # this model's small daily limit is used up: go straight to the fallback, do not waste a refused request
            try:
                self.store.bump_usage(kind, cap)
            except RuntimeError:  # this key's own daily cap
                if self._backup() is None:
                    raise
                return self._on_backup(call, m, "the content key's daily cap was reached")
            throttle(kind)
            throttle(f"{kind}:{m}", rpm_guard(m))
            count_model_use(self.store, kind, m)
            try:
                return with_retry(lambda: call(self.client, m))
            except errors.APIError as e:
                if e.code not in (429, 500, 503, 504):
                    raise
                last = e
        if last is None:
            raise RuntimeError("every configured model is at its daily limit")
        if last.code == 429 and self._backup() is not None:
            return self._on_backup(call, models[-1], "Google reported the content key's quota as spent")
        raise last

    def _on_backup(self, call, model: str, why: str):
        kind = self.cfg.content_backup_kind()
        self.store.bump_usage(kind, self.cfg.daily_content_backup_cap)  # raises when the backup's own cap is reached too
        throttle(kind)
        day = time.strftime("%Y-%m-%d")
        if self.store.get_kv("key_failover_logged", "") != day:  # once a day, so the log stays readable
            self.store.set_kv("key_failover_logged", day)
            self.store.add_log("key_failover", f"content request moved to the backup key ({self.cfg.content_backup_account() or 'unknown'}): {why}")
        return with_retry(lambda: call(self._backup(), model))

    def _json(self, prompt: str) -> dict[str, Any]:
        resp = self._generate(lambda c, m: c.models.generate_content(
            model=m,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.8),
        ), *self._json_models())
        return json.loads(resp.text)

    def assess_fit(self, profile: dict[str, Any], interests: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "You are a LinkedIn brand strategist. Compare the person's LinkedIn profile with the topics "
            "they want to write about. Return JSON: {\"score\": 0-100 integer, \"matching\": [..], "
            "\"gaps\": [..], \"angles\": [3-5 post angles that fit BOTH profile credibility and interests], "
            "\"profile_fixes\": [..]}.\n\n"
            f"PROFILE:\n{json.dumps(profile, indent=2)[:8000]}\n\nINTERESTS:\n{json.dumps(interests, indent=2)}"
        )
        return self._json(prompt)

    def draft_post(
        self,
        profile: dict[str, Any],
        interests: dict[str, Any],
        style: str,
        hook: str,
        top_posts: list[str],
        recent_posts: list[str],
        idea: dict[str, Any] | None = None,
        rules: list[str] | None = None,
        hints: list[str] | None = None,
        no_placeholders: bool = False,
        short: bool = False,
        banned_terms: list[str] | None = None,
        voice_block: str = "",
        formula_hint: str = "",
    ) -> dict[str, Any]:
        hints_block = (
            "YOUR PREVIOUS DRAFT WAS REJECTED. Fix every one of these problems:\n"
            + "\n".join(f"- {h}" for h in hints) + "\n\n"
        ) if hints else ""
        rules_block = (
            "AUDIENCE FEEDBACK RULES (learned from real comments on earlier posts - follow them, they override "
            "your default style):\n" + "\n".join(f"- {r}" for r in rules) + "\n\n"
        ) if rules else ""
        examples = "\n---\n".join(top_posts[:3]) or "(none yet)"
        recent = "\n---\n".join(recent_posts[:5]) or "(none yet)"
        topic = ""
        if idea:
            topic = (
                f"TOPIC: {idea['title']}\nBRIEF: {idea['brief']}\n"
                f"FACTS (the ONLY numbers/claims you may state as fact):\n{idea['facts'] or '(none supplied)'}\n"
                "Never invent figures, dates, names or quotes. "
                + ("Do NOT write [TBD] or any other placeholder, and do not invent a number to fill the gap: write the "
                   "sentence without the figure, or leave that detail out entirely. The post must read as finished text. "
                   if no_placeholders else
                   "Where a needed number is missing, write [TBD: what is needed] so the author can fill it in. ")
                + ("" if (idea["facts"] or "").strip() else
                   "This topic has no verified facts: never claim personal experience, results or numbers about the author's own "
                   "work (no 'I recently...', 'cut my time by half') unless the BRIEF states it. Teach from widely documented "
                   "behaviour and write from the reader's point of view. ")
                + "For stocks/IPOs: education and analysis only, no buy/sell/hold "
                "advice or price targets, label GMP as unofficial, and end with a one-line 'not investment advice' note.\n\n"
            )
        prompt = (
            topic + rules_block + hints_block + voice_block + formula_hint +
            "Write one LinkedIn post for this person, in their voice.\n"
            f"STYLE: {style} - {STYLES[style]}\nHOOK: {hook} - {HOOKS[hook]}\n"
            "Rules: first line must stop the scroll; short paragraphs; never use em dashes or en dashes; no hashtags spam (max 3 at the end); "
            "no clickbait lies; concrete details over generalities; " + ("900-1300 characters, ONE idea, a few short paragraphs, no numbered list; " if short else "900-1500 characters; ") + "do not repeat "
            "topics from RECENT POSTS; never touch the NO-GO topics.\n"
            + (f"NEVER mention any of these words or names: {', '.join(banned_terms)}.\n" if banned_terms else "") +
            "Return JSON: {\"text\": str, \"image_prompt\": str}. The image_prompt describes a clean, "
            "professional image that supports the post and contains NO text or letters.\n\n"
            f"PROFILE:\n{json.dumps(profile, indent=2)[:6000]}\n\nINTERESTS:\n{json.dumps(interests, indent=2)}\n\n"
            f"BEST PAST POSTS (imitate what works):\n{examples}\n\nRECENT POSTS (avoid repeating):\n{recent}"
        )
        return self._json(prompt)

    def review(
        self,
        text: str,
        image_prompt: str | None,
        image: tuple[bytes, str] | None,
        profile: dict[str, Any] | None,
        interests: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Critique a draft (and its image, if given) as a LinkedIn growth editor."""
        prompt = (
            "You are a blunt LinkedIn growth editor. Review this draft post"
            + (" and the attached image" if image else "")
            + ". Return JSON: {\"score\": 0-10, \"verdict\": one sentence, \"hook\": critique of the first line, "
            "\"strengths\": [..], \"weaknesses\": [..], \"suggested_rewrite_of_first_line\": str, "
            "\"fit_with_profile_and_interests\": str, \"image_feedback\": str (match to the post, clarity at "
            "feed size, any text/artifacts; 'no image' if none), \"publish_ready\": bool}.\n\n"
            f"POST:\n{text}\n\nINTENDED IMAGE PROMPT: {image_prompt or '(none)'}\n\n"
            f"PROFILE:\n{json.dumps(profile, indent=2)[:4000] if profile else '(unknown)'}\n\n"
            f"INTERESTS:\n{json.dumps(interests, indent=2) if interests else '(unknown)'}"
        )
        contents: list[Any] = [prompt]
        if image:
            contents.append(types.Part.from_bytes(data=image[0], mime_type=image[1]))
        resp = self._generate(lambda c, m: c.models.generate_content(
            model=m,
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4),
        ), self.cfg.gemini_text_model)
        return json.loads(resp.text)

    def suggest_hooks(self, post: str, topic: str, n: int = 5) -> dict[str, Any]:
        """Alternative openings for a draft. One Gemini call, only when asked for (never part of the autopilot)."""
        prompt = (
            f"Write {n} alternative opening lines for this LinkedIn post, each different in pattern (question, "
            "plain-English claim, short story moment, contrast, confession). Rules: plain English first, never open "
            "with a bare number or currency; max 150 characters; no em or en dashes; no links, @mentions or hashtags; "
            "no filler like 'excited to share'; do not invent facts beyond the post. "
            "Return JSON: {\"hooks\": [{\"pattern\": str, \"line\": str, \"why\": str}]}.\n\n"
            f"TOPIC: {topic or '(infer from the post)'}\n\nPOST:\n{post[:3000]}"
        )
        resp = self._generate(lambda c, m: c.models.generate_content(
            model=m, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.9)), self.cfg.gemini_text_model)
        return json.loads(resp.text)

    def analyze(self, rows: list[dict[str, Any]]) -> str:
        resp = self._generate(lambda c, m: c.models.generate_content(
            model=m,
            contents=(
                "Summarize in under 200 words what these LinkedIn post results say about which styles and "
                "hooks work and what to try next. Be honest about small sample sizes.\n"
                + json.dumps(rows, indent=2)
            ),
        ), self.cfg.gemini_text_model)
        return resp.text
