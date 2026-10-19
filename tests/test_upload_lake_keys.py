"""Tests for the S3 key sanitization in scripts/upload_lake_to_minio.py.

SURIMI naming convention (project wiki): directories in the shared bucket are
lower_snake_case. The local SURIMI-DATA-LAKE tree carries spaces, '&',
parentheses, '++', dots and SCREAMING CASE in directory names; the uploader
must sanitize every key segment instead of replicating the tree verbatim.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upload_lake_to_minio.py"


@pytest.fixture(scope="module")
def uploader():
    spec = importlib.util.spec_from_file_location("upload_lake_to_minio", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_module_imports_without_aws_env(monkeypatch):
    # Regression: module-level os.environ["AWS_ENDPOINT_URL"] made the script
    # (and any import of it) crash without creds. Env must resolve lazily.
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    spec = importlib.util.spec_from_file_location("upload_lake_smoke", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must not raise


def test_segment_lowercases_and_snakes_spaces(uploader):
    assert uploader.sanitize_key_segment("1.1_FAO LANDINGS FAO 37") == "1_1_fao_landings_fao_37"


def test_segment_ampersand_becomes_and(uploader):
    assert (
        uploader.sanitize_key_segment("8.1_EU DCF economic report & data")
        == "8_1_eu_dcf_economic_report_and_data"
    )


def test_segment_parens_and_dashes(uploader):
    assert (
        uploader.sanitize_key_segment("8.2_EU DCF social data - clean (STECF 22_06)")
        == "8_2_eu_dcf_social_data_clean_stecf_22_06"
    )


def test_segment_plus_plus_and_dots(uploader):
    assert uploader.sanitize_key_segment("CMSY++") == "cmsy"
    assert uploader.sanitize_key_segment("4.1_ICES.STOCK.DATA") == "4_1_ices_stock_data"


def test_segment_non_ascii_hyphen(uploader):
    # U+2010 HYPHEN as found in the wiki mirror filenames
    assert uploader.sanitize_key_segment("gRPC-messages-and-models-‐-matrix") == (
        "grpc_messages_and_models_matrix"
    )


def test_segment_idempotent_on_compliant_names(uploader):
    assert uploader.sanitize_key_segment("eu_dcf_aer_capacity") == "eu_dcf_aer_capacity"


def test_relative_key_sanitizes_dirs_and_stem_keeps_extension(uploader):
    rel = Path("8_Economic and Social/8.1_EU DCF economic report & data/README AER economic.txt")
    assert uploader.sanitize_relative_key(rel) == (
        "8_economic_and_social/8_1_eu_dcf_economic_report_and_data/readme_aer_economic.txt"
    )


def test_relative_key_lowercases_extension(uploader):
    assert uploader.sanitize_relative_key(Path("A/B.CSV")) == "a/b.csv"


def test_paths_overridable_via_env(monkeypatch):
    monkeypatch.setenv("SURIMI_LAKE_ROOT", "/data/lake")
    monkeypatch.setenv("SURIMI_WIKI_ROOT", "/data/wiki")
    spec = importlib.util.spec_from_file_location("upload_lake_env", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.LAKE == "/data/lake"
    assert mod.DOC_DIRS[0][0] == "/data/wiki"
