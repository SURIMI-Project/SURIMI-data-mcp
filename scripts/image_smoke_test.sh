#!/usr/bin/env bash
# Smoke-test a built SURIMI data server image. Acceptance for the deploy artifact:
#   1. the surimi_data_mcp package, its engine modules and its deps import cleanly,
#   2. all 20 tools are registered in the MCP, including aggregate, search_documents and query_data,
#   3. the bootstrap CLI exits nonzero when no data source is set,
#   4. no data is baked into the image (/app/data/exports has no CSVs).
# Usage: image_smoke_test.sh <image:tag>
set -uo pipefail
IMG="${1:?usage: image_smoke_test.sh <image:tag>}"
fail=0
pass(){ echo "  PASS: $1"; }
bad(){ echo "  FAIL: $1"; fail=1; }

echo "== smoke-testing $IMG =="

# 1. modules + deps + server import
if docker run --rm -i "$IMG" python - <<'PY' >/dev/null 2>&1
import boto3, duckdb, yaml  # noqa
from surimi_data_mcp.engine import aggregate, cube  # noqa
from surimi_data_mcp.knowledge import docsearch  # noqa
from surimi_data_mcp.loader import bootstrap  # noqa
from surimi_data_mcp import server  # noqa
PY
then pass "package, modules and deps import (engine, knowledge, loader, server, boto3, duckdb, yaml)"
else bad "modules+deps import"; fi

# 2. aggregate tool registered
if docker run --rm -i "$IMG" python - <<'PY' >/dev/null 2>&1
import asyncio
from surimi_data_mcp import server
names = [t.name for t in asyncio.run(server.mcp.list_tools())]
assert len(names) == 20, names
assert "aggregate" in names, names
assert "search_documents" in names and "query_data" in names, names
PY
then pass "20 tools registered, including aggregate, search_documents and query_data"
else bad "the 20 tools are not all registered"; fi

# 3. bootstrap CLI exits nonzero without SURIMI_DATA_SRC
# A missing module also exits nonzero, so this step only counts when step 1 passed.
if docker run --rm "$IMG" python -m surimi_data_mcp.loader.bootstrap >/dev/null 2>&1
then bad "bootstrap CLI should exit nonzero with no data source"
else pass "bootstrap CLI exits nonzero with no data source"; fi

# 4. no baked CSVs
n=$(docker run --rm "$IMG" sh -c 'ls /app/data/exports/*.csv 2>/dev/null | wc -l' 2>/dev/null | tr -d '[:space:]')
if [ "${n:-0}" = "0" ]; then pass "no baked CSVs in /app/data/exports"; else bad "found $n baked CSVs (data must not be in the image)"; fi

echo "== $([ "$fail" -eq 0 ] && echo 'ALL PASS' || echo 'FAILURES') =="
exit "$fail"
