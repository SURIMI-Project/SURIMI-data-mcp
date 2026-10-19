"""/sources/lake: a read-only copy of the SURIMI data lake, served by exact path from a manifest.

The whitelist is the `files` list of lake_manifest.json and nothing else, so traversal strings,
paths that are not in the manifest and files that exist on the pod all answer 404. The manifest
maps each ORIGINAL lake path (the one the audit scripts use) to the sanitized S3 key.
"""
import hashlib
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from surimi_data_mcp.web import lake_routes
from surimi_data_mcp.web.lake_routes import make_lake_routes

BODY = b"year,value\n2019,1\n2020,2\n" * 50


def _manifest(tmp_path, files):
    (tmp_path / "lake_manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # these tests are about serving; sign-in has its own file (test_lake_auth.py)
    monkeypatch.setenv("SURIMI_LAKE_ALLOW_ANONYMOUS", "true")
    lake_routes.reset()
    yield
    lake_routes.reset()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    key = "8_economic_and_social/aer_economic_national_level.csv"
    (tmp_path / key).parent.mkdir(parents=True)
    (tmp_path / key).write_bytes(BODY)
    _manifest(tmp_path, [{"path": "8_Economic and Social/AER_economic_national level.csv",
                          "key": key, "size": len(BODY),
                          "sha256": hashlib.sha256(BODY).hexdigest()}])
    monkeypatch.setenv("SURIMI_LAKE_DIR", str(tmp_path))
    monkeypatch.delenv("SURIMI_LAKE_S3", raising=False)
    return TestClient(Starlette(routes=make_lake_routes()))


def test_index_lists_the_files_with_size_and_checksum(client):
    r = client.get("/sources/lake")
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 1 and body["bytes"] == len(BODY)
    assert body["files"][0]["path"] == "8_Economic and Social/AER_economic_national level.csv"
    assert body["files"][0]["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert "key" not in body["files"][0]


def test_a_file_downloads_by_its_original_path(client):
    r = client.get("/sources/lake/8_Economic and Social/AER_economic_national level.csv")
    assert r.status_code == 200 and r.content == BODY
    assert r.headers["content-length"] == str(len(BODY))


def test_an_encoded_path_downloads_too(client):
    r = client.get("/sources/lake/8_Economic%20and%20Social/AER_economic_national%20level.csv")
    assert r.status_code == 200 and r.content == BODY


def test_a_range_request_returns_that_slice(client):
    r = client.get("/sources/lake/8_Economic and Social/AER_economic_national level.csv",
                   headers={"Range": "bytes=5-14"})
    assert r.status_code == 206
    assert r.content == BODY[5:15]
    assert r.headers["content-range"] == f"bytes 5-14/{len(BODY)}"


@pytest.mark.parametrize("bad", ["../etc/passwd", "8_Economic and Social/../../x",
                                 "nope.csv", "8_Economic and Social"])
def test_unknown_or_traversal_paths_are_404(client, bad):
    assert client.get(f"/sources/lake/{bad}").status_code == 404


def test_a_manifest_entry_cannot_point_outside_the_lake_dir(tmp_path, monkeypatch):
    (tmp_path.parent / "secret.txt").write_text("x", encoding="utf-8")
    _manifest(tmp_path, [{"path": "a.csv", "key": "../secret.txt", "size": 1, "sha256": "x"}])
    monkeypatch.setenv("SURIMI_LAKE_DIR", str(tmp_path))
    c = TestClient(Starlette(routes=make_lake_routes()))
    assert c.get("/sources/lake/a.csv").status_code == 404


def test_no_manifest_means_503_with_a_plain_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_DIR", str(tmp_path))
    c = TestClient(Starlette(routes=make_lake_routes()))
    r = c.get("/sources/lake")
    assert r.status_code == 503 and "not published" in r.json()["error"]
    assert c.get("/sources/lake/a.csv").status_code == 503


def test_unconfigured_server_says_so(monkeypatch):
    monkeypatch.delenv("SURIMI_LAKE_DIR", raising=False)
    monkeypatch.delenv("SURIMI_LAKE_S3", raising=False)
    c = TestClient(Starlette(routes=make_lake_routes()))
    assert c.get("/sources/lake").status_code == 503


class _FakeS3:
    """Records the key and Range it was asked for, answers from memory."""
    def __init__(self, objects):
        self.objects, self.calls = objects, []

    def get_object(self, Bucket, Key, **kw):
        self.calls.append((Bucket, Key, kw.get("Range")))
        if Key not in self.objects:
            raise KeyError(Key)
        data = self.objects[Key]
        if kw.get("Range"):
            a, b = kw["Range"].split("=")[1].split("-")
            data = data[int(a):int(b) + 1]
        body = type("B", (), {"iter_chunks": lambda s, chunk_size: iter([data]),
                              "read": lambda s: data})()
        return {"Body": body, "ContentLength": len(data)}


def test_s3_mode_streams_the_sanitized_key_and_passes_the_range(monkeypatch):
    key = "1_fisheries/eu_dcf_catches.csv"
    manifest = {"files": [{"path": "1_Fisheries/EU_DCF_Catches.csv", "key": key,
                           "size": len(BODY), "sha256": "x"}]}
    fake = _FakeS3({"mcp_data/lake/" + key: BODY,
                    "mcp_data/lake/lake_manifest.json": json.dumps(manifest).encode()})
    monkeypatch.delenv("SURIMI_LAKE_DIR", raising=False)
    monkeypatch.setenv("SURIMI_LAKE_S3", "s3://project-surimi/mcp_data/lake")
    monkeypatch.setattr(lake_routes, "_s3_client", lambda: fake)
    c = TestClient(Starlette(routes=make_lake_routes()))
    r = c.get("/sources/lake/1_Fisheries/EU_DCF_Catches.csv")
    assert r.status_code == 200 and r.content == BODY
    r = c.get("/sources/lake/1_Fisheries/EU_DCF_Catches.csv", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == BODY[:10]
    assert ("project-surimi", "mcp_data/lake/" + key, "bytes=0-9") in fake.calls


def test_s3_mode_only_ever_reads(monkeypatch):
    """The server holds read credentials for the whole bucket; it must never write."""
    src = open(lake_routes.__file__, encoding="utf-8").read()
    for verb in ("put_object", "delete_object", "upload_file", "copy_object", "delete_objects"):
        assert verb not in src, verb


def test_an_entry_with_its_own_prefix_is_read_from_there(monkeypatch):
    manifest = {"files": [{"path": "1_Fisheries/x.csv", "key": "1_Fisheries/x.csv", "size": 4, "sha256": "x",
                           "prefix": "mcp_data/lake_local"}]}
    fake = _FakeS3({"mcp_data/lake_local/1_Fisheries/x.csv": b"abcd",
                    "mcp_data/data/lake_manifest.json": json.dumps(manifest).encode()})
    monkeypatch.delenv("SURIMI_LAKE_DIR", raising=False)
    monkeypatch.setenv("SURIMI_LAKE_S3", "s3://project-surimi/mcp_data/data")
    monkeypatch.setattr(lake_routes, "_s3_client", lambda: fake)
    r = TestClient(Starlette(routes=make_lake_routes())).get("/sources/lake/1_Fisheries/x.csv")
    assert r.status_code == 200 and r.content == b"abcd"
    assert fake.calls[-1][1] == "mcp_data/lake_local/1_Fisheries/x.csv"
