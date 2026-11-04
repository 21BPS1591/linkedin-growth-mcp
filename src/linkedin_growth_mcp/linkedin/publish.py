"""Publish via the official LinkedIn Posts + Images APIs. Called only from the approval-gated tool."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import httpx

from ..config import Config
from .oauth import load_token

API = "https://api.linkedin.com/rest"
# Reserved characters in "little text" commentary; '#' is left alone so hashtags still work.
_RESERVED = re.compile(r"([\\|{}@\[\]()<>*_~])")


_DASH_SPACED = re.compile(r"\s*[\u2014\u2013]\s*")
_DASH_TIGHT = re.compile(r"(?<=\d)[\u2014\u2013](?=\d)")


def strip_dashes(text: str) -> str:
    """Posts never carry em/en dashes: ranges become hyphens, everything else a comma."""
    text = _DASH_TIGHT.sub("-", text)
    return _DASH_SPACED.sub(", ", text)


def escape_commentary(text: str) -> str:
    return _RESERVED.sub(r"\\\1", text)


_TAG = re.compile(r"(?<!\w)#\w+")


def with_brand_tag(text: str, brand: str, reach: str = "", limit: int = 5) -> str:
    """Series hashtag first, then the model's own tags, then broad-reach defaults, up to `limit` in total."""
    brand = brand.strip()
    if brand and not brand.startswith("#"):
        brand = "#" + brand
    body = _TAG.sub("", text).rstrip()
    wanted = [brand] if brand else []
    wanted += _TAG.findall(text)
    wanted += [t if t.startswith("#") else "#" + t for t in reach.split()]
    tags: list[str] = []
    for t in wanted:
        if t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    if not tags:
        return text
    return body + "\n\n" + " ".join(tags[:limit])


def _headers(cfg: Config, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": cfg.linkedin_api_version,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def _upload_image(cfg: Config, token: str, owner: str, image_path: Path) -> str:
    h = _headers(cfg, token)
    r = httpx.post(f"{API}/images?action=initializeUpload", headers=h, json={"initializeUploadRequest": {"owner": owner}}, timeout=30)
    r.raise_for_status()
    val = r.json()["value"]
    up = httpx.put(val["uploadUrl"], content=image_path.read_bytes(), headers={"Authorization": f"Bearer {token}"}, timeout=120)
    up.raise_for_status()
    return val["image"]


def publish_post(cfg: Config, text: str, image_path: str | None, ipo_series: bool = True,
                 tags: list[str] | None = None) -> tuple[str, str]:
    """Returns (post_urn, public_url). The IPO series/reach hashtags are only added to IPO posts; other posts get `tags`."""
    tok = load_token(cfg)
    author = f"urn:li:person:{tok['person_id']}"
    if ipo_series:
        text = with_brand_tag(text, cfg.brand_hashtag, cfg.reach_hashtags)
    elif tags:  # the draft's own tags are replaced by the chosen set, so the picker is what decides
        text = _TAG.sub("", text).rstrip() + "\n\n" + " ".join(tags)
    body: dict = {
        "author": author,
        "commentary": escape_commentary(strip_dashes(text)),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if image_path:
        body["content"] = {"media": {"id": _upload_image(cfg, tok["access_token"], author, Path(image_path)), "altText": "Post illustration"}}
    r = httpx.post(f"{API}/posts", headers=_headers(cfg, tok["access_token"]), json=body, timeout=60)
    r.raise_for_status()
    urn = r.headers.get("x-restli-id", "")
    if not urn:
        raise RuntimeError("LinkedIn accepted the post but returned no post id")
    return urn, f"https://www.linkedin.com/feed/update/{quote(urn, safe=':')}/"
