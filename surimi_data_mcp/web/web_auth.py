"""Browser sign-in for the data server, and the supporting files behind it at /sources/repo.

The lake (lake_auth.py) takes a bearer token, which suits scripts. A person following a citation in a
deliverable arrives with a browser, so this adds the browser half of the same sign-in: an OpenID Connect
authorization code flow with PKCE against the SURIMI Keycloak realm (which brokers EDITO login), ending
in a signed session cookie. The lake accepts that cookie too.

    /sources/auth/login?next=/sources/repo/   start sign-in, come back to `next`
    /sources/auth/callback                    Keycloak returns here
    /sources/auth/logout                      drop the session
    /sources/repo/                            index of the supporting files (audit-repro/)
    /sources/repo/<path>                      one file; Markdown is rendered, scripts shown as text

Everything lives under /sources because the EDITO ingress forwards only named prefixes to this server.
The client is public (no secret) and PKCE protects the code exchange. The session cookie is signed with
a key derived from `SURIMI_SESSION_SECRET`, or from `SURIMI_RECEIPT_SECRET` when that is unset. It carries
no token, lasts eight hours, and is HttpOnly, Secure and SameSite=Lax. Without an issuer or a signing
secret, sign-in and the files are unavailable (503).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import anyio
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                                 RedirectResponse, Response)
from starlette.routing import Route

from surimi_data_mcp.web import lake_auth

SESSION_COOKIE = "surimi_session"
LOGIN_COOKIE = "surimi_login"
SESSION_SECONDS = 8 * 3600
LOGIN_SECONDS = 600
DEFAULT_NEXT = "/sources/repo/"
_TEXT_SUFFIXES = {".py", ".txt", ".csv", ".json", ".sh", ".yaml", ".yml", ".tsv"}


def _client_id() -> str:
    return os.environ.get("SURIMI_WEB_OIDC_CLIENT", "surimi-data-mcp-web")


def _repo_dir() -> Path:
    return Path(os.environ.get("SURIMI_REPO_DIR", "/app/audit-repro")).resolve()


def _key() -> Optional[bytes]:
    """Return the key that signs the cookies, or None when no secret is set."""
    base = os.environ.get("SURIMI_SESSION_SECRET") or os.environ.get("SURIMI_RECEIPT_SECRET")
    return hmac.new(base.encode(), b"surimi-web-session", hashlib.sha256).digest() if base else None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(obj: dict) -> str:
    payload = _b64(json.dumps(obj, separators=(",", ":")).encode())
    return payload + "." + _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())


def _unsign(value: Optional[str]) -> Optional[dict]:
    key = _key()
    if not value or not key or "." not in value:
        return None
    payload, sig = value.rsplit(".", 1)
    good = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        return None
    try:
        obj = json.loads(_unb64(payload))
    except ValueError:
        return None
    return obj if obj.get("exp", 0) > time.time() else None


def session_claims(request: Request) -> Optional[dict]:
    """The signed-in user of this browser, or None."""
    return _unsign(request.cookies.get(SESSION_COOKIE))


def _safe_next(value: Optional[str]) -> str:
    """Return `value` when it is a path under `/sources/` or `/audit/`, otherwise the default `next` page."""
    if value and value.startswith(("/sources/", "/audit/")) and not value.startswith("//"):
        return value
    return DEFAULT_NEXT


def _redirect_uri(request: Request) -> str:
    base = os.environ.get("SURIMI_PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/sources/auth/callback"


def _post_form(url: str, form: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                 headers={"User-Agent": "surimi-data-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def _cookie(resp: Response, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, max_age=max_age, path="/", httponly=True, secure=True, samesite="lax")


def _unavailable() -> Response:
    return JSONResponse({"error": "sign-in is not configured on this server"}, status_code=503)


async def login(request: Request) -> Response:
    """Redirect the browser to the Keycloak login, with a PKCE challenge and a signed cookie that holds the state."""
    issuer = lake_auth._issuer()
    if not issuer or not _key():
        return _unavailable()
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(24)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    query = urllib.parse.urlencode({
        "client_id": _client_id(), "response_type": "code", "scope": "openid email profile",
        "redirect_uri": _redirect_uri(request), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    resp = RedirectResponse(f"{issuer}/protocol/openid-connect/auth?{query}", status_code=302)
    _cookie(resp, LOGIN_COOKIE, _sign({"state": state, "verifier": verifier,
                                       "next": _safe_next(request.query_params.get("next")),
                                       "exp": int(time.time()) + LOGIN_SECONDS}), LOGIN_SECONDS)
    return resp


async def callback(request: Request) -> Response:
    """Exchange the Keycloak code for a token, check it, set the session cookie and redirect to `next`."""
    issuer = lake_auth._issuer()
    if not issuer or not _key():
        return _unavailable()
    pending = _unsign(request.cookies.get(LOGIN_COOKIE))
    state = request.query_params.get("state", "")
    if not pending or not hmac.compare_digest(state, pending.get("state", "")):
        return PlainTextResponse("sign-in could not be completed: please start again", status_code=400)
    tok = await anyio.to_thread.run_sync(_post_form, f"{issuer}/protocol/openid-connect/token", {
        "grant_type": "authorization_code", "code": request.query_params.get("code", ""),
        "redirect_uri": _redirect_uri(request), "client_id": _client_id(),
        "code_verifier": pending["verifier"]})
    claims, denied = lake_auth.check_token(tok.get("access_token", ""), audience=_client_id())
    if denied is not None:
        return denied
    resp = RedirectResponse(pending.get("next") or DEFAULT_NEXT, status_code=302)
    _cookie(resp, SESSION_COOKIE, _sign({
        "sub": claims.get("sub"), "user": claims.get("preferred_username") or claims.get("email"),
        "roles": sorted(lake_auth._roles(claims)), "exp": int(time.time()) + SESSION_SECONDS}),
        SESSION_SECONDS)
    resp.delete_cookie(LOGIN_COOKIE, path="/")
    return resp


async def logout(request: Request) -> Response:
    """Delete the session cookie."""
    resp = PlainTextResponse("signed out")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _need_session(request: Request) -> Optional[Response]:
    """Return None for a valid session with the required role, otherwise the response to send.

    The response is 503 without sign-in configured, 403 for a missing role, a redirect to the login
    for a browser, and 401 for anything else.
    """
    if not lake_auth._issuer() or not _key():
        return _unavailable()
    s = session_claims(request)
    role = os.environ.get("SURIMI_LAKE_OIDC_ROLE", "").strip()
    if s and (not role or role in s.get("roles", [])):
        return None
    if s:
        return PlainTextResponse("your account is signed in but not allowed to see these files",
                                 status_code=403)
    if "text/html" in request.headers.get("accept", ""):
        target = urllib.parse.quote(request.url.path, safe="/")
        return RedirectResponse(f"/sources/auth/login?next={target}", status_code=302)
    return JSONResponse({"error": "sign-in required: open this address in a browser to sign in"},
                        status_code=401)


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><style>
body{{font:16px/1.55 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1d2a33}}
table{{border-collapse:collapse;margin:1rem 0}}td,th{{border:1px solid #c9d3da;padding:.3rem .6rem;text-align:left}}
pre,code{{background:#f3f6f8}}pre{{padding:.8rem;overflow:auto}}header{{color:#5b6b76;font-size:.9rem}}
</style></head><body><header>SURIMI data server, supporting files. Signed in as {user}.
<a href="/sources/repo/">All files</a> &middot; <a href="/sources/auth/logout">Sign out</a></header>{body}</body></html>"""


def _page(request: Request, title: str, body: str) -> HTMLResponse:
    user = html.escape((session_claims(request) or {}).get("user") or "")
    return HTMLResponse(_PAGE.format(title=html.escape(title), user=user, body=body))


async def repo_index(request: Request) -> Response:
    """List the supporting files in `SURIMI_REPO_DIR` for a signed-in user, leaving out names that start with `.` or `__`."""
    if (denied := _need_session(request)) is not None:
        return denied
    root = _repo_dir()
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                   if p.is_file() and not any(part.startswith((".", "__")) for part in p.relative_to(root).parts))
    items = "".join(f'<li><a href="/sources/repo/{urllib.parse.quote(f)}">{html.escape(f)}</a></li>'
                    for f in files)
    return _page(request, "Supporting files", f"<h1>Supporting files</h1><ul>{items}</ul>")


async def repo_file(request: Request) -> Response:
    """Send one supporting file: Markdown rendered as a page, known text types as plain text, others as a download."""
    if (denied := _need_session(request)) is not None:
        return denied
    root = _repo_dir()
    rel = request.path_params["path"]
    target = (root / rel).resolve()
    if root not in target.parents or not target.is_file() or any(
            part.startswith((".", "__")) for part in target.relative_to(root).parts):
        return PlainTextResponse("not found", status_code=404)
    if target.suffix == ".md":
        import markdown
        body = markdown.markdown(target.read_text(encoding="utf-8"),
                                 extensions=["tables", "fenced_code"])
        return _page(request, target.name, body)
    if target.suffix in _TEXT_SUFFIXES:
        return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"))
    return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")


def make_web_routes() -> list[Route]:
    """Return the sign-in routes under `/sources/auth` and the supporting-file routes under `/sources/repo`."""
    return [Route("/sources/auth/login", login, methods=["GET"]),
            Route("/sources/auth/callback", callback, methods=["GET"]),
            Route("/sources/auth/logout", logout, methods=["GET"]),
            Route("/sources/repo/", repo_index, methods=["GET"]),
            Route("/sources/repo", repo_index, methods=["GET"]),
            Route("/sources/repo/{path:path}", repo_file, methods=["GET"])]
