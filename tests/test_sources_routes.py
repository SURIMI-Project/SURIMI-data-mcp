"""Tests for /sources routes: manifest index + whitelisted file downloads.

The whitelist is exactly the `file` values in sources_manifest.json; anything
else (existing pod files, traversal strings, malicious manifest entries that
escape SURIMI_DATA_DIR) must 404.
"""
import json
import sys
import types

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from surimi_data_mcp.provenance import lineage
from surimi_data_mcp.web.sources_routes import make_sources_routes


@pytest.fixture(autouse=True)
def _reset_lineage():
    yield
    lineage.reset()


def _make_client(tmp_path, monkeypatch, manifest):
    mpath = tmp_path / "sources_manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(mpath))
    monkeypatch.setenv("SURIMI_DATA_DIR", str(tmp_path))
    lineage.reset()
    return TestClient(Starlette(routes=make_sources_routes()))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    csv = tmp_path / "ds1.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    return _make_client(tmp_path, monkeypatch, {
        "ds1": {"file": "ds1.csv", "sha256": "x", "n_rows": 1, "agency": "FAO"},
    })


def test_index_lists_manifest(client):
    r = client.get("/sources")
    assert r.status_code == 200
    assert r.json()["ds1"]["file"] == "ds1.csv"


def test_download_streams_file(client):
    r = client.get("/sources/ds1.csv")
    assert r.status_code == 200
    assert r.text == "a,b\n1,2\n"
    assert "text/csv" in r.headers["content-type"]


def test_non_manifest_file_is_404_even_if_exists(client, tmp_path):
    (tmp_path / "secret.txt").write_text("nope")
    assert client.get("/sources/secret.txt").status_code == 404


def test_path_traversal_rejected(client):
    assert client.get("/sources/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_nested_manifest_path_downloads(tmp_path, monkeypatch):
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "x.csv").write_text("c,d\n3,4\n", encoding="utf-8")
    c = _make_client(tmp_path, monkeypatch, {
        "ds2": {"file": "subdir/x.csv", "sha256": "y", "n_rows": 1, "agency": "FAO"},
    })
    r = c.get("/sources/subdir/x.csv")
    assert r.status_code == 200
    assert r.text == "c,d\n3,4\n"


def test_nested_traversal_rejected(tmp_path, monkeypatch):
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "x.csv").write_text("c,d\n3,4\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope")
    c = _make_client(tmp_path, monkeypatch, {
        "ds2": {"file": "subdir/x.csv", "sha256": "y", "n_rows": 1, "agency": "FAO"},
    })
    assert c.get("/sources/subdir%2F..%2F..%2Fsecret.txt").status_code == 404


def test_malicious_manifest_entry_rejected(tmp_path, monkeypatch):
    # Even when the traversal string IS whitelisted by a (compromised)
    # manifest, the resolved path must stay under SURIMI_DATA_DIR.
    c = _make_client(tmp_path, monkeypatch, {
        "evil": {"file": "../../etc/hostname", "sha256": "z",
                 "n_rows": 1, "agency": "FAO"},
    })
    assert c.get("/sources/../../etc/hostname").status_code == 404
    assert c.get("/sources/..%2F..%2Fetc%2Fhostname").status_code == 404


def test_absolute_manifest_entry_rejected(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, {
        "evil": {"file": "/etc/hostname", "sha256": "z",
                 "n_rows": 1, "agency": "FAO"},
    })
    assert c.get("/sources//etc/hostname").status_code == 404


class _FakeS3Body:
    def iter_chunks(self, chunk_size=None):
        return iter([b"s3,data\n1,2\n"])


class _FakeS3Client:
    def __init__(self, calls):
        self._calls = calls

    def get_object(self, Bucket, Key):
        self._calls.append((Bucket, Key))
        return {"Body": _FakeS3Body()}


def _patch_boto3(monkeypatch):
    """Install a fake boto3 module capturing (Bucket, Key) of get_object."""
    calls: list[tuple[str, str]] = []
    fake = types.ModuleType("boto3")
    fake.client = lambda *a, **k: _FakeS3Client(calls)
    monkeypatch.setitem(sys.modules, "boto3", fake)
    return calls


def test_s3_key_joins_prefix_and_filename(tmp_path, monkeypatch):
    # File whitelisted but absent locally -> falls through to S3; the key
    # must be `<prefix>/<filename>`.
    calls = _patch_boto3(monkeypatch)
    monkeypatch.setenv("SURIMI_SOURCES_S3", "s3://bkt/mcp_data/tables")
    c = _make_client(tmp_path, monkeypatch, {
        "ds9": {"file": "ds9.csv", "sha256": "s", "n_rows": 1, "agency": "FAO"},
    })
    r = c.get("/sources/ds9.csv")
    assert r.status_code == 200
    assert r.content == b"s3,data\n1,2\n"
    assert calls == [("bkt", "mcp_data/tables/ds9.csv")]


def test_s3_key_without_prefix(tmp_path, monkeypatch):
    calls = _patch_boto3(monkeypatch)
    monkeypatch.setenv("SURIMI_SOURCES_S3", "s3://bkt")
    c = _make_client(tmp_path, monkeypatch, {
        "raw": {"file": "blob.unknownext", "sha256": "s",
                "n_rows": 1, "agency": "FAO"},
    })
    r = c.get("/sources/blob.unknownext")
    assert r.status_code == 200
    assert calls == [("bkt", "blob.unknownext")]
    # unknown extension -> generic binary content type
    assert r.headers["content-type"].startswith("application/octet-stream")


def test_s3_key_nested_filename_keeps_subdir(tmp_path, monkeypatch):
    calls = _patch_boto3(monkeypatch)
    monkeypatch.setenv("SURIMI_SOURCES_S3", "s3://bkt/mcp_data/tables/")
    c = _make_client(tmp_path, monkeypatch, {
        "dsn": {"file": "subdir/x.csv", "sha256": "s",
                "n_rows": 1, "agency": "FAO"},
    })
    r = c.get("/sources/subdir/x.csv")
    assert r.status_code == 200
    assert calls == [("bkt", "mcp_data/tables/subdir/x.csv")]


def test_whitelisted_but_missing_locally_and_no_s3_is_404(tmp_path, monkeypatch):
    # Manifest lists it, but the file is not on disk and no S3 fallback set.
    monkeypatch.delenv("SURIMI_SOURCES_S3", raising=False)
    c = _make_client(tmp_path, monkeypatch, {
        "ds3": {"file": "gone.csv", "sha256": "w", "n_rows": 1, "agency": "FAO"},
    })
    assert c.get("/sources/gone.csv").status_code == 404


def test_s3_endpoint_prefers_minio_vars(monkeypatch):
    # Regression: _s3_stream used to read only AWS_S3_ENDPOINT_URL, but the chart
    # (and loader/bootstrap.py) set AWS_ENDPOINT_URL for MinIO. With the wrong var unset,
    # boto3 fell back to real AWS S3 and every /sources/<file> 404'd. The endpoint
    # resolver must honour the MinIO vars the deployment actually sets.
    from surimi_data_mcp.web import sources_routes
    for v in ("AWS_ENDPOINT_URL", "S3_ENDPOINT_URL", "AWS_S3_ENDPOINT_URL"):
        monkeypatch.delenv(v, raising=False)
    # chart sets AWS_ENDPOINT_URL -> must be picked up
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://minio.dive.edito.eu")
    assert sources_routes._s3_endpoint_url() == "https://minio.dive.edito.eu"
    # S3_ENDPOINT_URL also honoured when it is the one set
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://minio.example")
    assert sources_routes._s3_endpoint_url() == "https://minio.example"
    # legacy var still works as a fallback
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "https://legacy.example")
    assert sources_routes._s3_endpoint_url() == "https://legacy.example"
