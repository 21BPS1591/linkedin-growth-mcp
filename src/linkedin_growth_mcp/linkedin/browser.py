"""Patchright persistent-context wrapper: read-only, serialized, throttled, own account only."""
from __future__ import annotations

import asyncio
import json
import random

from patchright.async_api import BrowserContext, async_playwright

from ..config import Config
from ..store.db import Store

FEED_URL = "https://www.linkedin.com/feed/"


class LoginExpired(RuntimeError):
    """The saved LinkedIn login was redirected to a login or checkpoint page."""


class LinkedInBrowser:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._lock = asyncio.Lock()  # one page operation at a time

    async def _context(self, pw, headless: bool) -> BrowserContext:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(self.cfg.browser_profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        # Cookies exported on another OS (see export_session) are loaded here, since Chromium's
        # on-disk cookie encryption is OS-specific and a copied profile dir would not decrypt.
        if self.cfg.session_path.exists():
            await ctx.add_cookies(json.loads(self.cfg.session_path.read_text()))
        return ctx

    async def export_session(self) -> int:
        """Write the logged-in LinkedIn cookies to a portable JSON file (e.g. for the Docker container)."""
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.cfg.browser_profile_dir), headless=True
            )
            try:
                cookies = [c for c in await ctx.cookies() if "linkedin.com" in c["domain"]]
            finally:
                await ctx.close()
        if not any(c["name"] == "li_at" for c in cookies):
            raise RuntimeError("No LinkedIn login found in the local profile; run --login first")
        self.cfg.session_path.write_text(json.dumps(cookies))
        try:
            self.cfg.session_path.chmod(0o600)
        except OSError:
            pass
        return len(cookies)

    async def login_interactive(self, timeout_s: int = 300) -> bool:
        """Open a visible browser; user signs in by hand. Returns True once the feed loads."""
        async with async_playwright() as pw:
            ctx = await self._context(pw, headless=False)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.linkedin.com/login")
            try:
                await page.wait_for_url("**/feed/**", timeout=timeout_s * 1000)
                ok = True
            except Exception:
                ok = False
            await ctx.close()
            if ok:
                self.cfg.session_path.unlink(missing_ok=True)  # a stale exported session must never override a fresh login
            return ok

    async def read_text(self, url: str, selector: str = "main", scroll: int = 3) -> str:
        """Visit a URL and return the visible text of `selector`. Counts against the daily page cap."""
        async with self._lock:
            self.store.bump_usage("browser_page", self.cfg.daily_browser_page_cap)
            async with async_playwright() as pw:
                ctx = await self._context(pw, headless=True)
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.goto(url, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    if "/login" in page.url or "/checkpoint" in page.url or "authwall" in page.url:
                        raise LoginExpired(
                            "LinkedIn session is not logged in (or hit a checkpoint). Run `--login` again."
                        )
                    for _ in range(scroll):
                        await page.mouse.wheel(0, random.randint(600, 1000))
                        await asyncio.sleep(random.uniform(0.8, 1.8))
                    return await page.inner_text(selector)
                finally:
                    await ctx.close()
