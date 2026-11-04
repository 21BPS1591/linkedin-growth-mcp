"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    gemini_api_key: str
    gemini_text_model: str
    gemini_image_model: str
    linkedin_client_id: str
    linkedin_client_secret: str
    linkedin_redirect_uri: str
    linkedin_api_version: str
    mcp_auth_token: str
    max_posts_per_week: int
    daily_gemini_call_cap: int
    daily_browser_page_cap: int
    extraction_passes: int = 1  # RHP extraction requests per IPO
    brand_hashtag: str = "#IPOByTheNumbers"  # appended to every post so the whole series is searchable
    reach_hashtags: str = "#IPO #StockMarket #IndianStockMarket #Finance"  # broad tags for wider reach
    post_hour: int = 9  # local hour after which the daily post may go out
    alerts: bool = False  # desktop alerts for a missed post or a failure (on via ALERTS in the real config)
    market_sync: bool = False  # hourly chittorgarh.com sync inside the autopilot (on via MARKET_SYNC in the real config)
    attach_images: bool = False  # IPO posts are text-only unless this is on
    auto_experiments: bool = True  # after 2026-11-04 the autopilot runs, judges and keeps/reverts format experiments itself
    gemini_draft_model: str = ""  # model for post drafting; empty = same as gemini_text_model
    pillar_series: tuple[str, ...] = ("story", "bi_features", "career", "ai")  # non-IPO topics allowed to auto-post; add one at a time after reviewing results
    post_hours: tuple[int, ...] = (9,)  # candidate posting hours; more than one turns on the posting-time bandit
    editor_gate: bool = True  # run the Gemini editor on a draft that passed the deterministic gates
    editor_min_score: float = 7.0  # below this the draft is rewritten once, then the better-scoring version is used
    ipo_max_per_week: int = 2  # quality over quantity: at most this many IPO posts in any 7 days
    ipo_min_quality: float = 6.0  # an IPO must score at least this (0-10, autopilot.ipo_quality) to get a post
    comment_watch_hours: int = 36  # how long after publishing a post is checked for new comments
    comment_check_minutes: int = 120  # minimum gap between two comment checks (each one costs a browser page)
    # One Gemini key per kind of work, so one key's quota is never spent on another's job. The account fields only record
    # which Google account owns each key, for tracking; they are never used to sign in. There is NO fallback between keys.
    gemini_account: str = ""            # content key (GEMINI_API_KEY)
    gemini_image_api_key: str = ""
    gemini_image_account: str = ""
    gemini_extraction_api_key: str = ""
    gemini_extraction_account: str = ""
    gemini_insights_api_key: str = ""
    gemini_insights_account: str = ""
    daily_image_call_cap: int = 4
    # Optional backup for CONTENT generation only: name of another key ("image") used when the content key's daily cap is
    # reached or Google refuses it. Its calls are counted separately, against daily_content_backup_cap.
    gemini_content_backup: str = ""
    # Model routing (decided 2026-10-31 from a bake-off on real filings and drafts). A fallback model is tried on the SAME key
    # when the main model is refused or overloaded, before any backup key is considered.
    gemini_draft_fallback_model: str = ""
    gemini_extraction_model: str = ""           # empty = gemini_text_model
    gemini_extraction_fallback_model: str = ""
    daily_content_backup_cap: int = 60
    daily_extraction_call_cap: int = 10
    daily_insights_call_cap: int = 8

    @property
    def gemini_usage_kind(self) -> str:
        """Usage-counter name that changes with the API key, so a new key starts from zero."""
        import hashlib

        return "gemini-" + hashlib.sha1(self.gemini_api_key.encode()).hexdigest()[:6]

    def key_for(self, purpose: str) -> str:
        return {"content": self.gemini_api_key, "image": self.gemini_image_api_key,
                "extraction": self.gemini_extraction_api_key, "insights": self.gemini_insights_api_key}[purpose]

    def content_backup_key(self) -> str:
        return self.key_for(self.gemini_content_backup) if self.gemini_content_backup in ("image", "extraction", "insights") else ""

    def content_backup_account(self) -> str:
        return self.account_for(self.gemini_content_backup) if self.content_backup_key() else ""

    def content_backup_kind(self) -> str:
        import hashlib

        return "gemini-contentbackup-" + hashlib.sha1(self.content_backup_key().encode()).hexdigest()[:6]

    def account_for(self, purpose: str) -> str:
        return {"content": self.gemini_account, "image": self.gemini_image_account,
                "extraction": self.gemini_extraction_account, "insights": self.gemini_insights_account}[purpose]

    def cap_for(self, purpose: str) -> int:
        return {"content": self.daily_gemini_call_cap, "image": self.daily_image_call_cap,
                "extraction": self.daily_extraction_call_cap, "insights": self.daily_insights_call_cap}[purpose]

    def usage_kind(self, purpose: str) -> str:
        """Per-key usage counter. The content counter keeps its old name so today's count is not reset."""
        import hashlib

        if purpose == "content":
            return self.gemini_usage_kind
        return f"gemini-{purpose}-" + hashlib.sha1(self.key_for(purpose).encode()).hexdigest()[:6]

    @property
    def extraction_model(self) -> str:
        return self.gemini_extraction_model or self.gemini_text_model

    @property
    def draft_model(self) -> str:
        return self.gemini_draft_model or self.gemini_text_model

    @property
    def db_path(self) -> Path:
        return self.data_dir / "growth.db"

    @property
    def images_dir(self) -> Path:
        return Path(_env("IMAGES_DIR", str(self.data_dir / "images")))

    @property
    def filings_dir(self) -> Path:
        return Path(_env("FILINGS_DIR", str(self.data_dir / "filings")))

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "linkedin_session.json"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "linkedin_token.json"


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader so running from the project folder needs no extra tooling."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config() -> Config:
    _load_dotenv(PROJECT_ROOT / ".env")
    data_dir = Path(_env("DATA_DIR", str(Path.home() / ".linkedin-growth-mcp")))
    cfg = Config(
        data_dir=data_dir,
        gemini_api_key=_env("GEMINI_API_KEY"),
        gemini_account=_env("GEMINI_ACCOUNT"),
        gemini_image_api_key=_env("GEMINI_IMAGE_API_KEY"),
        gemini_image_account=_env("GEMINI_IMAGE_ACCOUNT"),
        gemini_extraction_api_key=_env("GEMINI_EXTRACTION_API_KEY"),
        gemini_extraction_account=_env("GEMINI_EXTRACTION_ACCOUNT"),
        gemini_insights_api_key=_env("GEMINI_INSIGHTS_API_KEY"),
        gemini_insights_account=_env("GEMINI_INSIGHTS_ACCOUNT"),
        daily_image_call_cap=int(_env("DAILY_IMAGE_CALL_CAP", "4") or 4),
        gemini_content_backup=_env("GEMINI_CONTENT_BACKUP").strip().lower(),
        gemini_draft_fallback_model=_env("GEMINI_DRAFT_FALLBACK_MODEL"),
        gemini_extraction_model=_env("GEMINI_EXTRACTION_MODEL"),
        gemini_extraction_fallback_model=_env("GEMINI_EXTRACTION_FALLBACK_MODEL"),
        daily_content_backup_cap=int(_env("DAILY_CONTENT_BACKUP_CAP", "60") or 60),
        daily_extraction_call_cap=int(_env("DAILY_EXTRACTION_CALL_CAP", "10") or 10),
        daily_insights_call_cap=int(_env("DAILY_INSIGHTS_CALL_CAP", "8") or 8),
        gemini_text_model=_env("GEMINI_TEXT_MODEL", "gemini-2.5-flash"),
        gemini_image_model=_env("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"),
        linkedin_client_id=_env("LINKEDIN_CLIENT_ID"),
        linkedin_client_secret=_env("LINKEDIN_CLIENT_SECRET"),
        linkedin_redirect_uri=_env("LINKEDIN_REDIRECT_URI", "http://localhost:8765/callback"),
        linkedin_api_version=_env("LINKEDIN_API_VERSION", "202610"),
        mcp_auth_token=_env("MCP_AUTH_TOKEN"),
        max_posts_per_week=int(_env("MAX_POSTS_PER_WEEK", "7")),
        daily_gemini_call_cap=int(_env("DAILY_GEMINI_CALL_CAP", "16")),  # free tier allows ~20/day per model
        daily_browser_page_cap=int(_env("DAILY_BROWSER_PAGE_CAP", "30")),
        extraction_passes=int(_env("EXTRACTION_PASSES", "1")),
        brand_hashtag=_env("BRAND_HASHTAG", "#IPOByTheNumbers"),
        reach_hashtags=_env("REACH_HASHTAGS", "#IPO #StockMarket #IndianStockMarket #Finance"),
        post_hour=int(_env("POST_HOUR", "9")),
        alerts=_env("ALERTS", "true").lower() in ("1", "true", "yes", "on"),
        market_sync=_env("MARKET_SYNC", "true").lower() in ("1", "true", "yes", "on"),
        attach_images=_env("ATTACH_IMAGES", "false").lower() in ("1", "true", "yes", "on"),
        auto_experiments=_env("AUTO_EXPERIMENTS", "true").lower() in ("1", "true", "yes", "on"),
        gemini_draft_model=_env("GEMINI_DRAFT_MODEL", ""),
        pillar_series=tuple(x.strip() for x in _env("PILLAR_SERIES", "story,bi_features,career,ai").split(",") if x.strip()),
        post_hours=tuple(sorted({int(x) for x in _env("POST_HOURS", _env("POST_HOUR", "9")).split(",") if x.strip()})),
        editor_gate=_env("EDITOR_GATE", "true").lower() in ("1", "true", "yes", "on"),
        editor_min_score=float(_env("EDITOR_MIN_SCORE", "7")),
        ipo_max_per_week=int(_env("IPO_MAX_PER_WEEK", "2")),
        ipo_min_quality=float(_env("IPO_MIN_QUALITY", "6")),
        comment_watch_hours=int(_env("COMMENT_WATCH_HOURS", "36")),
        comment_check_minutes=int(_env("COMMENT_CHECK_MINUTES", "120")),
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.images_dir.mkdir(parents=True, exist_ok=True)
    cfg.filings_dir.mkdir(parents=True, exist_ok=True)
    return cfg
