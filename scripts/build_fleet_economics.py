#!/usr/bin/env python3
"""Build the EU DCF fleet-economics export from the AER source in the lake.

    python3 scripts/build_fleet_economics.py <lake-root> <out-dir>

Writes ``hive_eu_dcf_fleet_economics.csv`` with one row per fleet segment and year,
using the STECF 2024 AER definitions described in ``surimi_data_mcp.loader.fleet_economics``.

When AUTH's ``Rev_GRP.csv`` is in the lake, the script compares the result with it
and exits with code 1 if any segment-year differs by more than 0.02.
"""
from __future__ import annotations

import collections
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from surimi_data_mcp.loader.fleet_economics import (  # noqa: E402
    DIMENSIONS,
    derive_fleet_economics,
    read_aer_rows,
    write_csv,
)

AER_SOURCE = (
    "8_Economic and Social/8.1_EU DCF economic report & data/"
    "AER_economic_fleet segment.csv"
)
AUTH_SOURCE = "10_Indicator Outputs/AUTH_Indicators/Rev_GRP.csv"
OUT_NAME = "hive_eu_dcf_fleet_economics.csv"


def reconcile(derived: list[dict], auth_csv: Path) -> list[tuple]:
    """Return a list of (key, AUTH value, our value) for each segment-year where AUTH's summed ``GRP`` differs from our ``gross_profit_excl_subsidies_and_rights`` by more than 0.02."""
    auth: dict[tuple, float] = collections.defaultdict(float)
    with open(auth_csv, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                auth[tuple(row[d] for d in DIMENSIONS)] += float(row["GRP"])
            except ValueError:
                continue
    mine = {
        tuple(str(r[d]) for d in DIMENSIONS):
        r["gross_profit_excl_subsidies_and_rights"]
        for r in derived
    }
    return [
        (k, auth[k], mine.get(k))
        for k in auth
        if abs(auth[k] - (mine.get(k) or 0.0)) > 0.02
    ]


def main(argv: list[str]) -> int:
    """Build the CSV from ``argv[1]`` (lake root) into ``argv[2]`` (output directory) and return the exit code: 0 on success, 1 on a mismatch with AUTH, 2 on wrong arguments."""
    if len(argv) != 3:
        print(__doc__)
        return 2
    lake, out_dir = Path(argv[1]), Path(argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    derived = derive_fleet_economics(read_aer_rows(str(lake / AER_SOURCE)))
    written = write_csv(derived, str(out_dir / OUT_NAME))
    unreported = sum(1 for r in derived if r["gross_profit"] is None)
    print(f"{OUT_NAME}: {written:,} fleet segment-years")
    print(f"  no economics reported (null, not zero): {unreported:,}")

    auth_csv = lake / AUTH_SOURCE
    if not auth_csv.exists():
        print("  AUTH Rev_GRP.csv absent, reconciliation skipped")
        return 0
    bad = reconcile(derived, auth_csv)
    print(f"  reconciled against AUTH Rev_GRP: {len(bad)} mismatches")
    for key, theirs, ours in bad[:10]:
        print(f"    {key}  auth={theirs!r}  ours={ours!r}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
