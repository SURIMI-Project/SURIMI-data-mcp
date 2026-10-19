"""The lake publisher plans keys and a manifest without touching the network, and writes only under mcp_data/."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_lake_copy as pub  # noqa: E402


def _lake(tmp_path):
    (tmp_path / "8_Economic and Social" / "8.1_EU DCF economic report & data").mkdir(parents=True)
    f = tmp_path / "8_Economic and Social" / "8.1_EU DCF economic report & data" / "AER_economic_national level.csv"
    f.write_bytes(b"a,b\n1,2\n")
    (tmp_path / "1_Fisheries").mkdir()
    (tmp_path / "1_Fisheries" / "EU_DCF_Catches.csv").write_bytes(b"x\n")
    (tmp_path / "1_Fisheries" / ".DS_Store").write_bytes(b"junk")
    return tmp_path


def test_plan_keeps_the_original_path_as_the_key_by_default(tmp_path):
    for e in pub.plan(_lake(tmp_path)):
        assert e["key"] == e["path"]


def test_plan_can_map_original_paths_to_sanitized_keys(tmp_path):
    entries = pub.plan(_lake(tmp_path), key_style="sanitized")
    by_path = {e["path"]: e for e in entries}
    e = by_path["8_Economic and Social/8.1_EU DCF economic report & data/AER_economic_national level.csv"]
    assert e["key"] == "8_economic_and_social/8_1_eu_dcf_economic_report_and_data/aer_economic_national_level.csv"
    assert e["size"] == 8
    assert e["sha256"] == hashlib.sha256(b"a,b\n1,2\n").hexdigest()


def test_hidden_files_are_not_published(tmp_path):
    paths = {e["path"] for e in pub.plan(_lake(tmp_path))}
    assert "1_Fisheries/.DS_Store" not in paths and len(paths) == 2


def test_two_paths_that_sanitize_to_one_key_abort(tmp_path):
    (tmp_path / "a b.csv").write_bytes(b"1")
    (tmp_path / "a_b.csv").write_bytes(b"2")
    with pytest.raises(SystemExit, match="same key"):
        pub.plan(tmp_path, key_style="sanitized")


def test_manifest_is_the_whitelist_the_server_reads(tmp_path):
    m = json.loads(pub.manifest_json(pub.plan(_lake(tmp_path))))
    assert set(m) == {"files"} and len(m["files"]) == 2
    assert all(set(f) == {"path", "key", "size", "sha256"} for f in m["files"])


@pytest.mark.parametrize("prefix", ["surimi_data", "", "netcdf/lake", "mcp_data", "../mcp_data/lake"])
def test_it_refuses_to_write_outside_mcp_data(prefix):
    with pytest.raises(SystemExit, match="mcp_data/"):
        pub.check_prefix(prefix)


def test_the_our_folder_prefix_is_accepted():
    assert pub.check_prefix("mcp_data/lake") == "mcp_data/lake"
    assert pub.check_prefix("mcp_data/lake/") == "mcp_data/lake"


def test_dry_run_makes_no_client(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pub, "_client", lambda: (_ for _ in ()).throw(AssertionError("network")))
    pub.main(["--lake", str(_lake(tmp_path)), "--dry-run"])
    out = capsys.readouterr().out
    assert "2 files" in out and "dry run" in out


class _Head:
    def __init__(self, sizes): self.sizes, self.writes = sizes, []
    def head_object(self, Bucket, Key):
        if Key not in self.sizes:
            err = KeyError(Key)
            err.response = {"Error": {"Code": "404"}}
            raise err
        return {"ContentLength": self.sizes[Key]}
    def put_object(self, **kw): self.writes.append(kw)


def test_manifest_only_writes_nothing_but_the_manifest_when_every_key_matches(tmp_path, monkeypatch, capsys):
    lake = _lake(tmp_path)
    entries = pub.plan(lake)
    fake = _Head({f"mcp_data/data/{e['key']}": e["size"] for e in entries})
    monkeypatch.setattr(pub, "_client", lambda: fake)
    pub.main(["--lake", str(lake), "--manifest-only"])
    assert [w["Key"] for w in fake.writes] == ["mcp_data/data/lake_manifest.json"]


def test_manifest_only_refuses_when_a_key_is_missing_or_differs(tmp_path, monkeypatch):
    lake = _lake(tmp_path)
    entries = pub.plan(lake)
    sizes = {f"mcp_data/data/{e['key']}": e["size"] for e in entries}
    sizes[f"mcp_data/data/{entries[0]['key']}"] += 1
    fake = _Head(sizes)
    monkeypatch.setattr(pub, "_client", lambda: fake)
    with pytest.raises(SystemExit, match="does not match"):
        pub.main(["--lake", str(lake), "--manifest-only"])
    assert fake.writes == []


def test_check_is_read_only(tmp_path, monkeypatch, capsys):
    lake = _lake(tmp_path)
    fake = _Head({})
    monkeypatch.setattr(pub, "_client", lambda: fake)
    pub.main(["--lake", str(lake), "--check"])
    assert fake.writes == [] and "missing 2" in capsys.readouterr().out


def test_a_failure_that_is_not_a_404_is_reported_as_an_error_not_as_missing(tmp_path, monkeypatch, capsys):
    class Denied(_Head):
        def head_object(self, Bucket, Key):
            err = RuntimeError("denied"); err.response = {"Error": {"Code": "403"}}; raise err
    monkeypatch.setattr(pub, "_client", lambda: Denied({}))
    pub.main(["--lake", str(_lake(tmp_path)), "--check"])
    out = capsys.readouterr().out
    assert "error 403 2" in out and "missing" not in out.split("same")[-1].split("error")[0].replace("missing 0", "")


class _Fill(_Head):
    def __init__(self, sizes):
        super().__init__(sizes); self.uploads = []
    def upload_file(self, path, bucket, key, **kw): self.uploads.append(key)


def test_fill_sends_missing_and_different_files_to_the_overlay_and_never_over_the_main_prefix(tmp_path, monkeypatch, capsys):
    lake = _lake(tmp_path)
    entries = pub.plan(lake)
    same, differs = entries[0], entries[1]
    sizes = {f"mcp_data/data/{same['key']}": same["size"], f"mcp_data/data/{differs['key']}": differs["size"] + 9}
    fake = _Fill(sizes)
    monkeypatch.setattr(pub, "_client", lambda: fake)
    pub.main(["--lake", str(lake), "--fill"])
    assert fake.uploads == [f"mcp_data/lake_local/{differs['key']}"]
    assert all(not k.startswith("mcp_data/data/") for k in fake.uploads)
    manifest = json.loads([w for w in fake.writes if w["Key"] == "mcp_data/data/lake_manifest.json"][0]["Body"])
    by = {f["path"]: f for f in manifest["files"]}
    assert "prefix" not in by[same["path"]] and by[differs["path"]]["prefix"] == "mcp_data/lake_local"


def test_the_overlay_must_not_overlap_the_main_prefix(tmp_path):
    with pytest.raises(SystemExit, match="separate"):
        pub.main(["--lake", str(_lake(tmp_path)), "--fill", "--overlay-prefix", "mcp_data/data/extra"])
