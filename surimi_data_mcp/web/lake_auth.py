"""Single sign-on for /sources/lake: check a Keycloak access token on every request.

The lake copy holds partner data, so it is never served openly. The server trusts one OpenID
issuer (the SURIMI Keycloak realm, which brokers EDITO login) and checks, on each request:
the signature against the issuer's published keys, the issuer, the expiry, optionally the
client (`aud` or `azp`) and optionally a role or group.

    SURIMI_LAKE_OIDC_ISSUER    e.g. https://surimi-ids.lab.dive.edito.eu/realms/surimi
    SURIMI_LAKE_OIDC_AUDIENCE  optional client id the token must be issued to
    SURIMI_LAKE_OIDC_ROLE      optional realm role, client role or group the user must hold
    SURIMI_LAKE_ALLOW_ANONYMOUS=true   explicit opt-out; without an issuer the lake refuses

Only asymmetric algorithms are accepted, so a token signed with a shared secret cannot pass.
The token is never logged or echoed.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
_HOW = "sign in first: python3 audit-repro/fetch_lake.py --login"


def _issuer() -> str:
    return os.environ.get("SURIMI_LAKE_OIDC_ISSUER", "").strip().rstrip("/")


@lru_cache(maxsize=4)
def _jwks_client(issuer: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{issuer}/protocol/openid-connect/certs", cache_keys=True,
                           lifespan=3600, timeout=10)


def _signing_key(token: str):
    return _jwks_client(_issuer()).get_signing_key_from_jwt(token)


def _roles(claims: dict) -> set[str]:
    """The realm roles, client roles and group names in a token's claims, as one set."""
    roles = set((claims.get("realm_access") or {}).get("roles") or [])
    for res in (claims.get("resource_access") or {}).values():
        roles |= set((res or {}).get("roles") or [])
    roles |= {g.lstrip("/") for g in claims.get("groups") or []}
    return roles


def _deny(status: int, message: str) -> Response:
    headers = {"WWW-Authenticate": 'Bearer realm="surimi-lake"'} if status == 401 else {}
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def check_token(token: str, audience: Optional[str] = None):
    """(claims, None) for an accepted token, else (None, the response to send).

    `audience` is the client the token must be issued to; None reads SURIMI_LAKE_OIDC_AUDIENCE."""
    issuer = _issuer()
    try:
        claims = jwt.decode(token, _signing_key(token).key, algorithms=_ALGS, issuer=issuer,
                            options={"require": ["exp", "iss"], "verify_aud": False})
    except Exception as exc:  # signature, expiry, issuer, malformed, key lookup
        logger.info("lake token rejected: %s", type(exc).__name__)
        return None, _deny(401, f"the token was not accepted ({type(exc).__name__}); {_HOW}")
    if audience is None:
        audience = os.environ.get("SURIMI_LAKE_OIDC_AUDIENCE", "").strip()
    if audience:
        aud = claims.get("aud")
        aud = [aud] if isinstance(aud, str) else list(aud or [])
        if audience not in aud and claims.get("azp") != audience:
            return None, _deny(401, f"the token was not issued for this service; {_HOW}")
    role = os.environ.get("SURIMI_LAKE_OIDC_ROLE", "").strip()
    if role and role not in _roles(claims):
        return None, _deny(403, "your account is signed in but not allowed to download the data lake")
    return claims, None


def guard(request: Request) -> Optional[Response]:
    """None when the request may proceed, otherwise the response to send.

    A bearer token (scripts) or the browser session set by web_auth (people) is accepted."""
    issuer = _issuer()
    if not issuer:
        if os.environ.get("SURIMI_LAKE_ALLOW_ANONYMOUS", "").lower() == "true":
            return None
        return _deny(503, "the data lake copy is switched on but sign-in is not configured, so it is not served")
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and header[7:].strip():
        return check_token(header[7:].strip())[1]
    from surimi_data_mcp.web import web_auth
    session = web_auth.session_claims(request)
    if session:
        role = os.environ.get("SURIMI_LAKE_OIDC_ROLE", "").strip()
        if role and role not in session.get("roles", []):
            return _deny(403, "your account is signed in but not allowed to download the data lake")
        return None
    return _deny(401, f"sign-in required; {_HOW}")
