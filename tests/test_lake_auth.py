"""/sources/lake is behind single sign-on: a Keycloak token is checked on every request.

Signed with a throwaway RSA key; the JWKS lookup is replaced so no network is touched. With the
lake switched on and no identity provider configured the route must refuse, never serve openly.
"""
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.testclient import TestClient

from surimi_data_mcp.web import lake_auth
from surimi_data_mcp.web import lake_routes
from surimi_data_mcp.web.lake_routes import make_lake_routes

ISSUER = "https://surimi-ids.lab.dive.edito.eu/realms/surimi"
BODY = b"year,value\n2019,1\n"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key=KEY, **over):
    claims = {"iss": ISSUER, "sub": "u1", "azp": "surimi-lake", "aud": "surimi-lake",
              "exp": int(time.time()) + 600, "preferred_username": "reader",
              "realm_access": {"roles": ["lake-reader"]}}
    claims.update(over)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256",
                      headers={"kid": "k1"})


class _Signing:
    def __init__(self, key): self.key = key.public_key()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    lake_routes.reset()
    monkeypatch.setattr(lake_auth, "_signing_key", lambda token: _Signing(KEY))
    for name in ("SURIMI_LAKE_OIDC_ISSUER", "SURIMI_LAKE_OIDC_AUDIENCE", "SURIMI_LAKE_OIDC_ROLE",
                 "SURIMI_LAKE_ALLOW_ANONYMOUS"):
        monkeypatch.delenv(name, raising=False)
    yield
    lake_routes.reset()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    key = "1_fisheries/x.csv"
    (tmp_path / key).parent.mkdir(parents=True)
    (tmp_path / key).write_bytes(BODY)
    (tmp_path / "lake_manifest.json").write_text(json.dumps({"files": [
        {"path": "1_Fisheries/x.csv", "key": key, "size": len(BODY), "sha256": "x"}]}), encoding="utf-8")
    monkeypatch.setenv("SURIMI_LAKE_DIR", str(tmp_path))
    return TestClient(Starlette(routes=make_lake_routes()))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_with_no_identity_provider_configured_the_lake_refuses(client):
    for path in ("/sources/lake", "/sources/lake/1_Fisheries/x.csv"):
        r = client.get(path)
        assert r.status_code == 503 and "sign-in is not configured" in r.json()["error"]


def test_the_anonymous_switch_is_explicit(client, monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_ALLOW_ANONYMOUS", "true")
    assert client.get("/sources/lake/1_Fisheries/x.csv").content == BODY


def test_no_token_is_401_and_says_how_to_sign_in(client, monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ISSUER", ISSUER)
    r = client.get("/sources/lake")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert "fetch_lake.py --login" in r.json()["error"]


def test_a_valid_token_downloads_and_lists(client, monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ISSUER", ISSUER)
    t = _token()
    assert client.get("/sources/lake", headers=_auth(t)).json()["n"] == 1
    assert client.get("/sources/lake/1_Fisheries/x.csv", headers=_auth(t)).content == BODY


def test_a_range_request_works_with_a_token(client, monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ISSUER", ISSUER)
    h = dict(_auth(_token()), Range="bytes=0-3")
    r = client.get("/sources/lake/1_Fisheries/x.csv", headers=h)
    assert r.status_code == 206 and r.content == BODY[:4]


@pytest.mark.parametrize("bad", [
    lambda: _token(key=OTHER),                      # signed by someone else
    lambda: _token(exp=int(time.time()) - 10),      # expired
    lambda: _token(iss="https://evil.example/realms/surimi"),   # wrong issuer
    lambda: "not.a.jwt",
    lambda: jwt.encode({"iss": ISSUER, "sub": "x"}, "secret", algorithm="HS256"),  # alg confusion
])
def test_bad_tokens_are_401(client, monkeypatch, bad):
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ISSUER", ISSUER)
    r = client.get("/sources/lake/1_Fisheries/x.csv", headers=_auth(bad()))
    assert r.status_code == 401 and r.content != BODY


def test_the_audience_is_checked_when_configured(client, monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("SURIMI_LAKE_OIDC_AUDIENCE", "surimi-lake")
    assert client.get("/sources/lake", headers=_auth(_token())).status_code == 200
    assert client.get("/sources/lake", headers=_auth(_token(aud="superset", azp="superset"))).status_code == 401
    # Keycloak often puts the client in azp and leaves aud as "account"
    assert client.get("/sources/lake", headers=_auth(_token(aud="account", azp="surimi-lake"))).status_code == 200


def test_a_required_role_or_group_is_enforced_with_403(client, monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ROLE", "lake-reader")
    assert client.get("/sources/lake", headers=_auth(_token())).status_code == 200
    r = client.get("/sources/lake", headers=_auth(_token(realm_access={"roles": ["other"]})))
    assert r.status_code == 403
    assert client.get("/sources/lake", headers=_auth(_token(realm_access=None, groups=["/lake-reader"]))).status_code == 200


def test_a_trailing_slash_in_the_issuer_setting_is_tolerated(client, monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ISSUER", ISSUER + "/")
    assert client.get("/sources/lake", headers=_auth(_token())).status_code == 200


def test_the_token_is_never_logged_or_echoed(client, monkeypatch, caplog):
    monkeypatch.setenv("SURIMI_LAKE_OIDC_ISSUER", ISSUER)
    t = _token(key=OTHER)
    r = client.get("/sources/lake", headers=_auth(t))
    assert t not in r.text and t not in caplog.text
