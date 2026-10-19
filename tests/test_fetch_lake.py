"""The reader's fetch helper selects by prefix, verifies checksums and skips what it already has."""
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audit-repro"))
import fetch_lake as fl  # noqa: E402

BODY = b"a,b\n1,2\n"
FILES = [{"path": "1_Fisheries/EU DCF/x.csv", "size": len(BODY), "sha256": hashlib.sha256(BODY).hexdigest()},
         {"path": "2_Abundance/y.csv", "size": 3, "sha256": "z"}]


def test_select_matches_a_file_or_a_folder_prefix_only():
    assert [f["path"] for f in fl.select(FILES, ["1_Fisheries"])] == ["1_Fisheries/EU DCF/x.csv"]
    assert [f["path"] for f in fl.select(FILES, ["2_Abundance/y.csv"])] == ["2_Abundance/y.csv"]
    assert fl.select(FILES, ["1_Fish"]) == []


def test_script_files_finds_the_files_a_script_names(tmp_path, monkeypatch):
    (tmp_path / "Q-E2-3.py").write_text('AER = "8.1_EU DCF/AER_economic_national level.csv"\n')
    monkeypatch.setattr(fl, "HERE", tmp_path)
    files = [{"path": "8_Economic and Social/8.1_EU DCF/AER_economic_national level.csv", "size": 1, "sha256": "x"},
             {"path": "2_Abundance/unrelated.csv", "size": 1, "sha256": "y"}]
    got = [f["path"] for f in fl.script_files("Q-E2-3", files)]
    assert got == ["8_Economic and Social/8.1_EU DCF/AER_economic_national level.csv"]


def test_a_download_is_written_and_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "_open", lambda url: io.BytesIO(BODY))
    assert fl.fetch("http://x", FILES[0], tmp_path) == "get"
    assert (tmp_path / FILES[0]["path"]).read_bytes() == BODY
    assert fl.fetch("http://x", FILES[0], tmp_path) == "skip"


def test_a_bad_checksum_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "_open", lambda url: io.BytesIO(b"tampered"))
    entry = dict(FILES[0], size=8)
    with pytest.raises(SystemExit, match="checksum"):
        fl.fetch("http://x", entry, tmp_path)
    assert not list(tmp_path.rglob("*.csv")) and not list(tmp_path.rglob("*.part"))


# ---- sign-in -------------------------------------------------------------------------------

import time  # noqa: E402


@pytest.fixture()
def tokfile(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "TOKEN_FILE", tmp_path / "cache" / "tok.json")
    return fl.TOKEN_FILE


def test_without_a_saved_token_the_user_is_told_to_log_in(tokfile):
    with pytest.raises(SystemExit, match="--login"):
        fl.bearer()


def test_a_valid_token_is_used_as_is(tokfile, monkeypatch):
    fl._save({"access_token": "A", "refresh_token": "R", "expires_in": 300})
    monkeypatch.setattr(fl, "_post", lambda *a, **k: pytest.fail("must not refresh"))
    assert fl.bearer() == "A"


def test_the_token_file_is_private(tokfile):
    fl._save({"access_token": "A", "expires_in": 300})
    assert oct(tokfile.stat().st_mode & 0o777) == "0o600"


def test_an_expiring_token_is_refreshed_and_saved(tokfile, monkeypatch):
    fl._save({"access_token": "old", "refresh_token": "R", "expires_in": 5})
    seen = {}
    def post(url, data):
        seen.update(data); return {"access_token": "new", "refresh_token": "R2", "expires_in": 300}
    monkeypatch.setattr(fl, "_post", post)
    assert fl.bearer() == "new" and seen["grant_type"] == "refresh_token" and seen["refresh_token"] == "R"
    assert json.loads(tokfile.read_text())["access_token"] == "new"


def test_a_failed_refresh_says_to_log_in_again(tokfile, monkeypatch):
    fl._save({"access_token": "old", "refresh_token": "R", "expires_in": 5})
    monkeypatch.setattr(fl, "_post", lambda *a: {"error": "invalid_grant"})
    with pytest.raises(SystemExit, match="expired"):
        fl.bearer()


def test_login_polls_until_the_user_has_signed_in(tokfile, monkeypatch, capsys):
    answers = iter([{"device_code": "D", "user_code": "ABCD-1234", "verification_uri_complete": "https://x/y",
                     "expires_in": 600, "interval": 1},
                    {"error": "authorization_pending"},
                    {"access_token": "A", "refresh_token": "R", "expires_in": 300}])
    monkeypatch.setattr(fl, "_post", lambda *a: next(answers))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fl.login()
    out = capsys.readouterr().out
    assert "https://x/y" in out and "ABCD-1234" in out
    assert json.loads(tokfile.read_text())["access_token"] == "A"


def test_login_reports_a_server_that_has_no_such_client(tokfile, monkeypatch):
    monkeypatch.setattr(fl, "_post", lambda *a: {"error": "invalid_client", "error_description": "no such client"})
    with pytest.raises(SystemExit, match="not available"):
        fl.login()
