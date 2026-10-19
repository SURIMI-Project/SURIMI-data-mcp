"""Guard: the image carries the whole server package and starts it as a package.

The Dockerfile used to copy an explicit list of top-level .py files, and a module missing from that
list shipped twice (metrics.py in v0.7.4, lake_routes.py later) as a container that crash-looped
with ModuleNotFoundError. Since the code moved into the surimi_data_mcp package (2026-10-12) the image
copies the package directory, so a new module is in the image by construction. These tests keep it
that way: the package is copied whole, the server starts with `python -m`, and no Python module
creeps back to the repository root.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()


def test_the_image_copies_the_whole_package():
    assert "COPY surimi_data_mcp/ surimi_data_mcp/" in DOCKERFILE


def test_the_image_starts_the_server_as_a_package():
    assert 'CMD ["python", "-m", "surimi_data_mcp"]' in DOCKERFILE


def test_no_python_module_sits_at_the_repository_root():
    stray = sorted(f for f in os.listdir(ROOT) if f.endswith(".py"))
    assert stray == [], f"move these into surimi_data_mcp/: {stray}"


def test_every_package_folder_is_a_package():
    pkg = os.path.join(ROOT, "surimi_data_mcp")
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if any(f.endswith(".py") for f in filenames):
            assert "__init__.py" in filenames, f"{dirpath} has modules but no __init__.py"
