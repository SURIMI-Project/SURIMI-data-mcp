"""A prebuilt cube must not be preferred over sources it no longer covers.

get_cube opens the prebuilt cube in SURIMI_CUBE_DB only when `cube_is_stale` accepts
it. Without that check, registrations accumulated in datasets.yaml, CSVs landed on the
volume, and the pod kept opening a cube built before any of it. Nine catalogued datasets answered "unknown dataset" on every call for weeks:
MEDITS, Traits, both ICES tables, both landings tables, the fuel-price view, EU
DCF catches and effort. Fixing it by hand took three rebuilds, two code fixes, a
volume expansion and five releases.

get_cube falls back to build_cube(SURIMI_DATA_DIR) when no prebuilt cube is present,
and `cube_is_stale` compares the two. A cube that does not cover a CSV sitting beside
it is stale, and the check is a directory listing against the cube's table list.

The check is not a content hash, so that it stays cheap at every boot. A missing
dataset is visible from names alone.
"""
from __future__ import annotations

import duckdb
import pytest

from surimi_data_mcp.engine import cube as cube_module


def _sources(tmp_path, names):
    d = tmp_path / "sources"
    d.mkdir()
    for n in names:
        (d / f"{n}.csv").write_text("year,v\n2020,1\n")
    return d


def test_a_cube_covering_every_source_is_reused(tmp_path):
    src = _sources(tmp_path, ["a", "b"])
    db = str(tmp_path / "cube.duckdb")
    cube_module.build_cube(str(src), db_path=db)
    assert cube_module.cube_is_stale(db, str(src)) is False


def test_a_cube_missing_a_source_is_stale(tmp_path):
    """The exact shape of the live failure: a CSV arrives, the cube predates it."""
    src = _sources(tmp_path, ["a"])
    db = str(tmp_path / "cube.duckdb")
    cube_module.build_cube(str(src), db_path=db)
    (src / "newly_registered.csv").write_text("year,v\n2021,2\n")
    assert cube_module.cube_is_stale(db, str(src)) is True


def test_an_absent_cube_is_stale(tmp_path):
    src = _sources(tmp_path, ["a"])
    assert cube_module.cube_is_stale(str(tmp_path / "nope.duckdb"), str(src)) is True


def test_an_absent_source_dir_is_not_stale(tmp_path):
    """The deployed pod may carry a cube and no raw CSVs at all. With nothing to
    compare against, reusing the cube is right; rebuilding is impossible."""
    db = str(tmp_path / "cube.duckdb")
    src = _sources(tmp_path, ["a"])
    cube_module.build_cube(str(src), db_path=db)
    assert cube_module.cube_is_stale(db, str(tmp_path / "gone")) is False


def test_an_unreadable_cube_is_stale(tmp_path):
    """Fail toward rebuilding, never toward serving something unopenable."""
    bad = tmp_path / "broken.duckdb"
    bad.write_bytes(b"not a database")
    src = _sources(tmp_path, ["a"])
    assert cube_module.cube_is_stale(str(bad), str(src)) is True


def test_get_cube_rebuilds_when_stale(tmp_path, monkeypatch):
    """The behaviour that matters: a newly arrived CSV becomes queryable on boot."""
    src = _sources(tmp_path, ["a"])
    db = str(tmp_path / "cube.duckdb")
    cube_module.build_cube(str(src), db_path=db)
    (src / "late_arrival.csv").write_text("year,v\n2021,2\n")
    monkeypatch.setenv("SURIMI_CUBE_DB", db)
    monkeypatch.setenv("SURIMI_DATA_DIR", str(src))
    cube_module._CUBE = None
    got = set(cube_module.get_cube().datasets())
    assert "late_arrival" in got, "a CSV beside the cube must be queryable after boot"
