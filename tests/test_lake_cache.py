"""The lake copy on the server's own volume, so the lake keeps working when the daily S3 token expires.

warm() copies each manifest file from S3 onto the volume, checked against its sha256; the manifest is
kept there too. Downloads come from the local copy when it is complete and from S3 otherwise.
"""
import hashlib
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from surimi_data_mcp.web import lake_cache
from surimi_data_mcp.web import lake_routes
from surimi_data_mcp.web.lake_routes import make_lake_routes

A, B = b"year,v\n2019,1\n" * 40, b"x,y\n" * 10


def _manifest(entries):
    return json.dumps({"files": entries}).encode()


def _entry(path, body, key=None, prefix=None):
    e = {"path": path, "key": key or path, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    if prefix:
        e["prefix"] = prefix
    return e


class FakeS3:
    def __init__(self, objects, fail=False):
        self.objects, self.fail, self.gets = objects, fail, []

    def get_object(self, Bucket, Key, **kw):
        self.gets.append(Key)
        if self.fail:
            raise RuntimeError("InvalidAccessKeyId")
        if Key not in self.objects:
            raise KeyError(Key)
        data = self.objects[Key]
        if kw.get("Range"):
            a, b = kw["Range"].split("=")[1].split("-")
            data = data[int(a):int(b) + 1]
        body = type("B", (), {"read": lambda s: data,
                              "iter_chunks": lambda s, chunk_size=1 << 20: iter([data])})()
        return {"Body": body, "ContentLength": len(data)}


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    entries = [_entry("1_Fisheries/a b.csv", A), _entry("2_Abundance/b.csv", B, prefix="mcp_data/lake_local")]
    s3 = FakeS3({"mcp_data/data/lake_manifest.json": _manifest(entries),
                 "mcp_data/data/1_Fisheries/a b.csv": A,
                 "mcp_data/lake_local/2_Abundance/b.csv": B})
    cache = tmp_path / "cache"
    monkeypatch.setenv("SURIMI_LAKE_S3", "s3://project-surimi/mcp_data/data")
    monkeypatch.setenv("SURIMI_LAKE_CACHE", str(cache))
    monkeypatch.setenv("SURIMI_LAKE_ALLOW_ANONYMOUS", "true")
    monkeypatch.delenv("SURIMI_LAKE_DIR", raising=False)
    monkeypatch.delenv("SURIMI_LAKE_OIDC_ISSUER", raising=False)
    monkeypatch.setattr(lake_routes, "_s3_client", lambda: s3)
    lake_routes.reset()
    yield s3, cache, entries
    lake_routes.reset()


def _client():
    return TestClient(Starlette(routes=make_lake_routes()))


def test_warm_copies_every_file_and_checks_it(setup):
    s3, cache, entries = setup
    stats = lake_cache.warm()
    assert stats == {"copied": 2, "present": 0, "failed": 0, "total": 2}
    assert (cache / "files" / "1_Fisheries" / "a b.csv").read_bytes() == A
    assert (cache / "files" / "2_Abundance" / "b.csv").read_bytes() == B
    assert json.loads((cache / "lake_manifest.json").read_text())["files"] == entries


def test_a_second_warm_copies_nothing(setup):
    lake_cache.warm()
    s3, *_ = setup
    s3.gets.clear()
    assert lake_cache.warm()["present"] == 2
    assert [k for k in s3.gets if not k.endswith("lake_manifest.json")] == []


def test_a_file_whose_checksum_differs_is_not_kept(setup):
    s3, cache, _ = setup
    s3.objects["mcp_data/data/1_Fisheries/a b.csv"] = A[:-1] + b"X"
    stats = lake_cache.warm()
    assert stats["failed"] == 1 and stats["copied"] == 1
    assert not (cache / "files" / "1_Fisheries" / "a b.csv").exists()
    assert not list(cache.rglob("*.part"))


def test_downloads_come_from_the_local_copy_once_it_is_there(setup):
    s3, cache, _ = setup
    lake_cache.warm()
    s3.gets.clear()
    r = _client().get("/sources/lake/1_Fisheries/a b.csv")
    assert r.status_code == 200 and r.content == A
    assert [k for k in s3.gets if not k.endswith("lake_manifest.json")] == []
    r = _client().get("/sources/lake/1_Fisheries/a b.csv", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == A[:10]


def test_with_s3_gone_the_lake_still_lists_and_serves_from_the_copy(setup):
    s3, cache, _ = setup
    lake_cache.warm()
    lake_routes.reset()
    s3.fail = True
    c = _client()
    idx = c.get("/sources/lake")
    assert idx.status_code == 200 and idx.json()["n"] == 2
    assert c.get("/sources/lake/2_Abundance/b.csv").content == B


def test_a_file_not_yet_copied_is_streamed_from_s3(setup):
    s3, cache, _ = setup
    r = _client().get("/sources/lake/2_Abundance/b.csv")
    assert r.status_code == 200 and r.content == B
    assert "mcp_data/lake_local/2_Abundance/b.csv" in s3.gets


def test_warm_stops_cleanly_when_s3_refuses(setup):
    s3, cache, _ = setup
    s3.fail = True
    stats = lake_cache.warm()
    assert stats["copied"] == 0 and stats["total"] == 0
    assert not list(cache.rglob("*.part"))


def test_a_manifest_path_cannot_write_outside_the_cache(setup):
    s3, cache, _ = setup
    evil = _entry("../../escape.csv", B)
    s3.objects["mcp_data/data/lake_manifest.json"] = _manifest([evil])
    s3.objects["mcp_data/data/../../escape.csv"] = B
    stats = lake_cache.warm()
    assert stats["copied"] == 0 and stats["failed"] == 1
    assert not (cache.parent / "escape.csv").exists()


def test_warm_skips_when_the_volume_is_too_small(setup, monkeypatch):
    s3, cache, _ = setup
    monkeypatch.setattr(lake_cache, "_free_bytes", lambda p: 10)
    stats = lake_cache.warm()
    assert stats["copied"] == 0 and stats.get("skipped") == "not enough free space"


def test_no_cache_configured_means_no_warm(setup, monkeypatch):
    monkeypatch.delenv("SURIMI_LAKE_CACHE")
    assert lake_cache.warm() is None
    assert lake_cache.local_file({"path": "1_Fisheries/a b.csv", "size": len(A)}) is None
