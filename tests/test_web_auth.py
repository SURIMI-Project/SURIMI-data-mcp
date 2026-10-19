"""Browser sign-in on the data server: Keycloak authorization code + PKCE, a signed session cookie,
and the supporting files at /sources/repo behind it. The lake accepts the same session."""
import base64
import hashlib
import json
import time
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.testclient import TestClient

from surimi_data_mcp.web import lake_auth
from surimi_data_mcp.web import lake_routes
from surimi_data_mcp.web import web_auth

ISSUER = "https://surimi-ids.lab.dive.edito.eu/realms/surimi"
BASE = "https://data.example.org"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Signing:
    key = KEY.public_key()


def _token(**over):
    c = {"iss": ISSUER, "sub": "u1", "azp": "surimi-data-mcp-web", "aud": "account",
         "exp": int(time.time()) + 300, "preferred_username": "reader@example.org",
         "realm_access": {"roles": ["default-roles-surimi"]}}
    c.update(over)
    return jwt.encode({k: v for k, v in c.items() if v is not None}, KEY, algorithm="RS256")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "evaluation").mkdir(parents=True)
    (repo / "EVALUATION.md").write_text("# Evaluation detail\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n")
    (repo / "Q-E2-3.py").write_text("print('ok')\n")
    (repo / "evaluation" / "fig.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "secret.txt").write_text("no")
    for k, v in {"SURIMI_LAKE_OIDC_ISSUER": ISSUER, "SURIMI_PUBLIC_BASE_URL": BASE,
                 "SURIMI_RECEIPT_SECRET": "x" * 64, "SURIMI_REPO_DIR": str(repo),
                 "SURIMI_LAKE_OIDC_ROLE": "default-roles-surimi"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SURIMI_LAKE_OIDC_AUDIENCE", raising=False)
    monkeypatch.setattr(lake_auth, "_signing_key", lambda token: _Signing())
    lake_routes.reset()
    return repo


def _client(follow=False):
    app = Starlette(routes=[*web_auth.make_web_routes(), *lake_routes.make_lake_routes()])
    return TestClient(app, base_url=BASE, follow_redirects=follow)


def _login(c, monkeypatch, token=None, next_path="/sources/repo/"):
    r = c.get("/sources/auth/login", params={"next": next_path})
    assert r.status_code == 302
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    seen = {}

    def fake_post(url, form):
        seen.update(form, url=url)
        return {"access_token": token or _token(), "expires_in": 300}
    monkeypatch.setattr(web_auth, "_post_form", fake_post)
    cb = c.get("/sources/auth/callback", params={"code": "abc", "state": q["state"][0]})
    return r, q, cb, seen


def test_the_login_redirect_uses_pkce_and_the_public_callback(env, monkeypatch):
    c = _client()
    r = c.get("/sources/auth/login", params={"next": "/sources/repo/"})
    loc = urllib.parse.urlparse(r.headers["location"])
    q = urllib.parse.parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{ISSUER}/protocol/openid-connect/auth"
    assert q["client_id"] == ["surimi-data-mcp-web"] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == [f"{BASE}/sources/auth/callback"] and q["response_type"] == ["code"]


def test_a_full_sign_in_sets_a_session_and_opens_the_repo(env, monkeypatch):
    c = _client()
    r, q, cb, seen = _login(c, monkeypatch)
    assert cb.status_code == 302 and cb.headers["location"] == "/sources/repo/"
    # the verifier sent at the token step matches the challenge sent at the start
    challenge = base64.urlsafe_b64encode(hashlib.sha256(seen["code_verifier"].encode()).digest()).rstrip(b"=")
    assert challenge.decode() == q["code_challenge"][0]
    assert seen["redirect_uri"] == f"{BASE}/sources/auth/callback"
    page = c.get("/sources/repo/EVALUATION.md")
    assert page.status_code == 200 and "<table>" in page.text
    assert c.get("/sources/repo/").status_code == 200
    assert "Q-E2-3.py" in c.get("/sources/repo/").text


def test_without_a_session_a_browser_is_sent_to_sign_in_and_a_script_gets_401(env):
    c = _client()
    r = c.get("/sources/repo/EVALUATION.md", headers={"accept": "text/html"})
    assert r.status_code == 302 and r.headers["location"].startswith("/sources/auth/login?next=")
    assert c.get("/sources/repo/EVALUATION.md", headers={"accept": "*/*"}).status_code == 401


def test_a_wrong_state_or_a_rejected_token_sets_no_session(env, monkeypatch):
    c = _client()
    c.get("/sources/auth/login", params={"next": "/sources/repo/"})
    monkeypatch.setattr(web_auth, "_post_form", lambda u, f: {"access_token": _token()})
    assert c.get("/sources/auth/callback", params={"code": "a", "state": "forged"}).status_code == 400
    assert c.get("/sources/repo/x", headers={"accept": "*/*"}).status_code == 401
    c2 = _client()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = jwt.encode({"iss": ISSUER, "exp": int(time.time()) + 60}, other, algorithm="RS256")
    _, _, cb, _ = _login(c2, monkeypatch, token=bad)
    assert cb.status_code == 401
    assert c2.get("/sources/repo/", headers={"accept": "*/*"}).status_code == 401


def test_a_user_without_the_required_role_is_refused(env, monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ROLE", "lake-reader")
    c = _client()
    _, _, cb, _ = _login(c, monkeypatch)
    assert cb.status_code == 403


def test_next_cannot_send_the_user_off_site(env, monkeypatch):
    c = _client()
    for bad in ("https://evil.example/x", "//evil.example/x", "/elsewhere"):
        _, _, cb, _ = _login(c, monkeypatch, next_path=bad)
        assert cb.headers["location"] == "/sources/repo/", bad


def test_a_tampered_or_expired_session_cookie_is_ignored(env, monkeypatch):
    c = _client()
    _login(c, monkeypatch)
    val = c.cookies.get(web_auth.SESSION_COOKIE)
    c.cookies.set(web_auth.SESSION_COOKIE, val[:-2] + ("AA" if not val.endswith("AA") else "BB"))
    assert c.get("/sources/repo/", headers={"accept": "*/*"}).status_code == 401
    c2 = _client()
    _login(c2, monkeypatch)
    monkeypatch.setattr(web_auth.time, "time", lambda: 10 ** 12)
    assert c2.get("/sources/repo/", headers={"accept": "*/*"}).status_code == 401


def test_repo_paths_cannot_escape_the_directory(env, monkeypatch):
    c = _client()
    _login(c, monkeypatch)
    for bad in ("../secret.txt", "evaluation/../../secret.txt", "%2e%2e/secret.txt"):
        assert c.get(f"/sources/repo/{bad}").status_code == 404, bad


def test_the_lake_accepts_the_browser_session(env, monkeypatch, tmp_path):
    lake = tmp_path / "lake"
    (lake / "a").mkdir(parents=True)
    (lake / "a" / "x.csv").write_bytes(b"1,2\n")
    (lake / "lake_manifest.json").write_text(json.dumps({"files": [
        {"path": "a/x.csv", "key": "a/x.csv", "size": 4, "sha256": "s"}]}))
    monkeypatch.setenv("SURIMI_LAKE_DIR", str(lake))
    c = _client()
    assert c.get("/sources/lake/a/x.csv").status_code == 401
    _login(c, monkeypatch)
    r = c.get("/sources/lake/a/x.csv")
    assert r.status_code == 200 and r.content == b"1,2\n"


def test_the_session_cookie_is_secure_httponly_and_lax(env, monkeypatch):
    c = _client()
    _, _, cb, _ = _login(c, monkeypatch)
    sc = [h for k, h in cb.headers.multi_items() if k == "set-cookie" and web_auth.SESSION_COOKIE in h][0].lower()
    assert "httponly" in sc and "secure" in sc and "samesite=lax" in sc and "path=/" in sc


def test_logout_clears_the_session(env, monkeypatch):
    c = _client()
    _login(c, monkeypatch)
    c.get("/sources/auth/logout")
    assert c.get("/sources/repo/", headers={"accept": "*/*"}).status_code == 401


def test_without_an_issuer_sign_in_is_unavailable(env, monkeypatch):
    monkeypatch.delenv("SURIMI_LAKE_OIDC_ISSUER")
    assert _client().get("/sources/auth/login").status_code == 503
    assert _client().get("/sources/repo/", headers={"accept": "*/*"}).status_code == 503
