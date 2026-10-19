#!/usr/bin/env python3
"""Download files of the SURIMI data lake from the data server into references/SURIMI-DATA-LAKE/.

    python3 fetch_lake.py --login                      # once: sign in with your EDITO account
    python3 fetch_lake.py --list                       # every file with its size
    python3 fetch_lake.py "1_Fisheries/1.3_EU DCF Catches"   # everything under a folder
    python3 fetch_lake.py --all                        # the whole lake, about 12 GB

Run it from the folder that will hold references/. The lake is behind single
sign-on: --login prints a link, you sign in with EDITO in your browser, and the token is kept in
~/.cache/surimi-lake-token.json (readable by you only) and refreshed when it runs out. A file that
is already there with the right size is skipped; every download is checked against the sha256 the
server lists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://project-surimi-surimi-data-mcp.lab.dive.edito.eu"
ISSUER = "https://surimi-ids.lab.dive.edito.eu/realms/surimi"
CLIENT = "surimi-lake"
TOKEN_FILE = Path(os.environ.get("SURIMI_LAKE_TOKEN_FILE", Path.home() / ".cache" / "surimi-lake-token.json"))
DEST = Path("references/SURIMI-DATA-LAKE")
_UA = {"User-Agent": "surimi-audit-repro/1.0"}
HERE = Path(__file__).resolve().parent


def _post(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def _save(tok: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tok = dict(tok, expires_at=time.time() + int(tok.get("expires_in", 300)))
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


def login(issuer: str = ISSUER, client: str = CLIENT) -> None:
    """Device-code sign-in: print a link, wait while the user signs in with EDITO in a browser."""
    oidc = f"{issuer}/protocol/openid-connect"
    dev = _post(f"{oidc}/auth/device", {"client_id": client, "scope": "openid"})
    if "device_code" not in dev:
        sys.exit(f"sign-in is not available: {dev.get('error_description') or dev}")
    print(f"Open {dev.get('verification_uri_complete') or dev['verification_uri']}")
    print(f"and confirm the code {dev['user_code']}. Waiting for you to sign in ...")
    deadline, wait = time.time() + int(dev.get("expires_in", 600)), int(dev.get("interval", 5))
    while time.time() < deadline:
        time.sleep(wait)
        tok = _post(f"{oidc}/token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                      "device_code": dev["device_code"], "client_id": client})
        if "access_token" in tok:
            _save(tok)
            print(f"signed in; token kept in {TOKEN_FILE}")
            return
        if tok.get("error") == "slow_down":
            wait += 5
        elif tok.get("error") != "authorization_pending":
            sys.exit(f"sign-in failed: {tok.get('error_description') or tok.get('error')}")
    sys.exit("sign-in timed out")


def bearer(issuer: str = ISSUER, client: str = CLIENT) -> str:
    """A valid access token, refreshed if it is about to run out; exits with the fix if there is none."""
    try:
        tok = json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        sys.exit("not signed in: run  python3 fetch_lake.py --login")
    if tok.get("expires_at", 0) - time.time() > 30:
        return tok["access_token"]
    new = _post(f"{issuer}/protocol/openid-connect/token", {
        "grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", ""), "client_id": client})
    if "access_token" not in new:
        sys.exit("your sign-in has expired: run  python3 fetch_lake.py --login")
    _save(new)
    return new["access_token"]


def _open(url: str):
    req = urllib.request.Request(url, headers=dict(_UA, Authorization=f"Bearer {bearer()}"))
    try:
        return urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("the server did not accept your sign-in: run  python3 fetch_lake.py --login")
        if e.code == 403:
            sys.exit("you are signed in, but your account is not allowed to download the lake")
        if e.code == 503:
            sys.exit("the data lake copy is not switched on on this server")
        raise


def index(base: str) -> list[dict]:
    """Return the server's list of lake files, each with its path, size and sha256."""
    with _open(f"{base}/sources/lake") as r:
        return json.load(r)["files"]


def script_files(question: str, files: list[dict]) -> list[dict]:
    """Lake files a script reads: those whose file name appears in the script's source.

    A script that scans a whole folder instead of naming files (Q-E2-4, Q-E3-1) names none of
    them; fetch those with a folder path or --all.
    """
    src = (HERE / f"{question}.py").read_text(encoding="utf-8")
    return [f for f in files if f["path"].rsplit("/", 1)[-1] in src]


def select(files: list[dict], wanted: list[str]) -> list[dict]:
    """Return the files whose path is one of `wanted` or lies under one of them."""
    return [f for f in files if any(f["path"] == w or f["path"].startswith(w.rstrip("/") + "/")
                                    for w in wanted)]


def fetch(base: str, entry: dict, dest: Path) -> str:
    """Download one lake file under `dest` and return "skip" if it was already there, else "get".

    A file of the same size is skipped. A download whose sha256 differs from the listed one is deleted and stops the run.
    """
    target = dest / entry["path"]
    if target.is_file() and target.stat().st_size == entry["size"]:
        return "skip"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    h = hashlib.sha256()
    with _open(f"{base}/sources/lake/{urllib.parse.quote(entry['path'])}") as r, open(tmp, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            f.write(chunk)
            h.update(chunk)
    if entry.get("sha256") and h.hexdigest() != entry["sha256"]:
        tmp.unlink()
        raise SystemExit(f"checksum mismatch for {entry['path']}")
    tmp.replace(target)
    return "get"


def main(argv: list[str] | None = None) -> None:
    """Run the command line: sign in, list the lake, or download the files that were asked for."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="a file or folder, as written in the lake")
    ap.add_argument("--for", dest="question", help="fetch what this audit script reads, e.g. Q-E2-3")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--login", action="store_true", help="sign in with your EDITO account (once)")
    ap.add_argument("--base", default=BASE)
    a = ap.parse_args(argv)
    if a.login:
        login()
        return
    files = index(a.base)
    if a.list:
        for f in files:
            print(f"{f['size']:>14,}  {f['path']}")
        return
    chosen = files if a.all else select(files, list(a.paths))
    if a.question:
        chosen += [f for f in script_files(a.question, files) if f not in chosen]
    if not chosen:
        sys.exit("nothing matched; try --list")
    total = sum(f["size"] for f in chosen)
    print(f"{len(chosen)} files, {total / 1e6:,.0f} MB -> {DEST}/")
    for f in chosen:
        print(f"  {fetch(a.base, f, DEST):4} {f['path']}")


if __name__ == "__main__":
    main()
