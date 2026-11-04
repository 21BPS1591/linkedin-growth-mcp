"""LinkedIn OAuth (authorization-code flow). No local callback server: the user pastes back the `code`."""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import Config

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
SCOPES = "openid profile w_member_social"


def auth_url(cfg: Config, state: str = "lgm") -> str:
    if not cfg.linkedin_client_id:
        raise RuntimeError("LINKEDIN_CLIENT_ID is not set")
    q = {
        "response_type": "code",
        "client_id": cfg.linkedin_client_id,
        "redirect_uri": cfg.linkedin_redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(q)}"


def exchange_code(cfg: Config, code: str) -> dict[str, Any]:
    r = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cfg.linkedin_client_id,
            "client_secret": cfg.linkedin_client_secret,
            "redirect_uri": cfg.linkedin_redirect_uri,
        },
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json()
    tok["obtained_at"] = time.time()
    me = httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30)
    me.raise_for_status()
    tok["person_id"] = me.json()["sub"]
    cfg.token_path.write_text(json.dumps(tok))
    try:
        cfg.token_path.chmod(0o600)
    except OSError:
        pass
    return {"person_id": tok["person_id"], "expires_in": tok.get("expires_in")}


def load_token(cfg: Config) -> dict[str, Any]:
    if not cfg.token_path.exists():
        raise RuntimeError("Not authorized with LinkedIn yet: call linkedin_auth_url, then linkedin_auth_complete")
    tok = json.loads(cfg.token_path.read_text())
    if time.time() > tok["obtained_at"] + int(tok.get("expires_in", 0)) - 60:
        raise RuntimeError("LinkedIn access token expired: re-run linkedin_auth_url / linkedin_auth_complete")
    return tok


def authorize_interactive(cfg: Config, timeout: int = 300) -> dict[str, Any]:
    """One-click sign-in: open the browser, catch the redirect on localhost, exchange the code for a token."""
    import http.server
    import secrets
    import time as _time
    import webbrowser
    from urllib.parse import parse_qs, urlparse

    state = secrets.token_urlsafe(16)
    redirect = urlparse(cfg.linkedin_redirect_uri)
    box: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path != redirect.path:
                self.send_response(404)
                self.end_headers()
                return
            q = parse_qs(u.query)
            if q.get("state", [""])[0] != state:
                box["error"] = "state mismatch"
            elif "code" in q:
                box["code"] = q["code"][0]
            else:
                box["error"] = q.get("error_description", ["authorization was denied"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"LinkedIn authorization received. You can close this tab.")

        def log_message(self, *args):  # keep the console quiet
            pass

    server = http.server.HTTPServer(("127.0.0.1", redirect.port or 80), Handler)
    server.timeout = 1
    try:
        webbrowser.open(auth_url(cfg, state))
        deadline = _time.time() + timeout
        while _time.time() < deadline and not box:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in box:
        raise RuntimeError(box.get("error", "timed out waiting for LinkedIn authorization"))
    return exchange_code(cfg, box["code"])
