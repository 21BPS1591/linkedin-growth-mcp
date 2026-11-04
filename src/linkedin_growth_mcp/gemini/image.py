"""Gemini image generation -> PNG file on disk."""
from __future__ import annotations

from pathlib import Path

from google import genai
from google.genai import types

from ..config import Config
from ..store.db import Store
from .keys import KeyMissing, throttle
from .retry import with_retry


class GeminiImage:
    def __init__(self, cfg: Config, store: Store):
        if not cfg.gemini_image_api_key:  # images use their own key; the content key is never a fallback
            raise KeyMissing("image")
        if cfg.cap_for("image") <= 0:
            raise RuntimeError("Image generation is switched off (DAILY_IMAGE_CALL_CAP=0). The image model is paid only: "
                               "turn billing on for its Google account, then set a cap above 0 in .env.")
        self.cfg = cfg
        self.store = store
        self.client = genai.Client(api_key=cfg.gemini_image_api_key, http_options=types.HttpOptions(timeout=180_000))

    def generate(self, prompt: str, out_name: str) -> Path:
        self.store.bump_usage(self.cfg.usage_kind("image"), self.cfg.cap_for("image"))
        throttle(self.cfg.usage_kind("image"))
        resp = with_retry(lambda: self.client.models.generate_content(
            model=self.cfg.gemini_image_model,
            contents=f"{prompt}\n\nLandscape 1200x627 composition, professional, no text or letters in the image.",
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        ))
        for cand in resp.candidates or []:
            for part in cand.content.parts or []:
                if getattr(part, "inline_data", None) and part.inline_data.data:
                    path = self.cfg.images_dir / f"{out_name}.png"
                    path.write_bytes(part.inline_data.data)
                    return path
        raise RuntimeError("Gemini returned no image (possibly blocked by safety filters); revise the prompt")
