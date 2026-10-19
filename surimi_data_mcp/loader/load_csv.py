"""Load the CSV exports into PostgreSQL, with a column type inferred from the values.

Each column becomes BIGINT, DOUBLE PRECISION or TEXT according to the values in it, so
that `SUM(employment_fte)` and `WHERE year IN (2017, 2020)` work without a cast.
Inference is conservative: codes with a leading zero, such as "007", and integer-valued
code columns stay TEXT. `CSV_TO_TABLE` lists the files that are loaded and the table each one goes to.
"""
import codecs
import csv
import os
import re

# The database address, read from DATABASE_URL. The code carries no default password, so set it before running main().
DB_URL = os.environ.get("DATABASE_URL", "")


# Run-together upstream headers that the general rule cannot split into words.
# CSV columns are lower_snake_case.
_HEADER_RENAMES = {
    "fromtable": "from_table",
}


def _sanitize_header(h: str) -> str:
    """Turn a CSV header such as "total.trips" or "average sea days" into a lower_snake_case SQL identifier.

    Every run of characters other than letters, digits and underscores becomes one
    underscore. A four-digit year from 1800 to 2199 gets the prefix ``y_``, any other
    name that starts with a digit gets ``col_``, and an empty header becomes ``col``.
    """
    s = re.sub(r"[^0-9A-Za-z_]+", "_", (h or "").strip())
    s = re.sub(r"_+", "_", s).strip("_").lower()
    s = _HEADER_RENAMES.get(s, s)
    if re.fullmatch(r"(18|19|20|21)\d{2}", s):
        s = "y_" + s
    elif s and s[0].isdigit():
        s = "col_" + s
    return s or "col"


def _sanitize_headers(headers: list[str]) -> list[str]:
    """Sanitize a header row. Repeated names get a numeric suffix, as in foo, foo_2, foo_3."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for h in headers:
        s = _sanitize_header(h)
        if s in seen:
            seen[s] += 1
            s = f"{s}_{seen[s]}"
        else:
            seen[s] = 1
        out.append(s)
    return out

CSV_TO_TABLE = {
    "hive_oecd_fsedata.csv": "oecd.fsedata",
    "hive_oecd_oecd_fisheries_support_estimates_eu_coastal_states.csv": "oecd.oecd_fisheries_support_estimates_eu_coastal_states",
    "hive_eu_dcf_eu_dcf_fisheries_economic_performance_aegean_sea.csv": "eu_dcf.eu_dcf_fisheries_economic_performance_aegean_sea",
    "hive_eu_dcf_eu_dcf_fisheries_employment_western_mediterranean.csv": "eu_dcf.eu_dcf_fisheries_employment_western_mediterranean",
    "hive_eu_dcf_aer_economic_national_level.csv": "eu_dcf.aer_economic_national_level",
    "hive_eu_dcf_aer_social_data_2017_2020.csv": "eu_dcf.aer_social_data_2017_2020",
    "hive_eu_dcf_aer_capacity.csv": "eu_dcf.aer_capacity",
    "hive_eu_dcf_aer_economic_2013_2023.csv": "eu_dcf.aer_economic_2013_2023",
    "hive_eu_dcf_fdi_capacity_by_country.csv": "eu_dcf.fdi_capacity_by_country",
    "hive_eu_dcf_fuel.csv": "eu_dcf.fuel",
    # Built by scripts/build_fleet_economics.py from the AER source. See fleet_economics.py.
    "hive_eu_dcf_fleet_economics.csv": "eu_dcf.fleet_economics",
    "hive_surimi_surimi_empirical_biomass_surveys_western_mediterranean.csv": "surimi.surimi_empirical_biomass_surveys_western_mediterranean",
    "hive_surimi_surimi_simulated_sales_output_aegean_sea.csv": "surimi.surimi_simulated_sales_output_aegean_sea",
    # DataGUI landings, in two tables. The ICES area-level file and the ICES
    # division-level file describe the same area-27 catch at two granularities,
    # so one combined table would count it twice. datasets/datasets.yaml warns to
    # sum from one table and never from both.
    "hive_fisheries_fisheries_landings.csv": "fisheries.fisheries_landings",
    "hive_fisheries_fisheries_landings_divisions.csv": "fisheries.fisheries_landings_divisions",
    # The keys follow the hive_<schema>_<table>.csv naming of the files under the
    # served prefix (s3://project-surimi/mcp_data/tables/). A key with any other
    # name finds no file there. The year columns of the MEDITS file become
    # y_1994..y_2021 through the header sanitizer. See the surimi-biomass-medits and
    # fishbase-sealifebase-traits entries in datasets.yaml and
    # tests/test_e6_medits_and_traits_registration.py.
    "hive_surimi_medits_survey_cpue_mediterranean.csv": "surimi.medits_survey_cpue_mediterranean",
    "hive_ecological_fishbase_sealifebase_traits.csv": "ecological.fishbase_sealifebase_traits",
    # ICES Stock Database export, all stocks, FAO Area 27. "latest" holds the most
    # recent assessment of each stock as a per-year time series, and "timeseries"
    # holds every assessment year. The source README is in
    # 4_Stock Assessment/4.1_ICES.STOCK.DATA.
    "hive_ices_stock_assessment_latest.csv": "ices.stock_assessment_latest",
    "hive_ices_stock_assessment_timeseries.csv": "ices.stock_assessment_timeseries",
}


# Text values read as missing (NULL). Without this a column that holds one of them would
# infer TEXT, or store NaN or Inf in PostgreSQL, and SUM and AVG would fail or return NaN.
#
# "c" is the EU DCF confidentiality placeholder ("confidential data are not shown, the
# placeholder C is used instead"), found in EU_DCF_Catches.csv and its effort file.
# "na" is the missing-value marker that R writes, as in the ICES stock-assessment export.
_NONFINITE = {
    "nan", "inf", "-inf", "+inf", "infinity", "-infinity", "+infinity",
    "c", "na",
}

_BIGINT_MIN = -(2 ** 63)
_BIGINT_MAX = 2 ** 63 - 1


class _ColTypeInference:
    """Infer the PostgreSQL type of one column from the values passed to `update`, one at a time.

    The column is BIGINT when every value is an integer that survives `int` and `str` unchanged, so "007" and "+5" do not count.
    It is DOUBLE PRECISION when every value parses as a float and at least one contains ``.``, ``e`` or ``E``. Otherwise it is TEXT.
    """

    def __init__(self) -> None:
        self.saw_value = False
        self.could_int = True
        self.could_float = True
        self.saw_fractional = False

    def update(self, v: str | None) -> None:
        """Add one raw value. Empty values and the `_NONFINITE` markers are ignored."""
        if v is None or v == "":
            return
        s = v.strip()
        if s.lower() in _NONFINITE:
            return
        self.saw_value = True
        if self.could_int:
            try:
                iv = int(s)
                if str(iv) != s or not (_BIGINT_MIN <= iv <= _BIGINT_MAX):
                    self.could_int = False
            except ValueError:
                self.could_int = False
        if self.could_float:
            try:
                float(s)
                if any(c in s for c in ".eE"):
                    self.saw_fractional = True
            except ValueError:
                self.could_float = False

    def resolve(self) -> str:
        """Return ``BIGINT``, ``DOUBLE PRECISION`` or ``TEXT`` for the values seen so far."""
        if not self.saw_value:
            return "TEXT"
        if self.could_int:
            return "BIGINT"
        if self.could_float and self.saw_fractional:
            return "DOUBLE PRECISION"
        return "TEXT"


def infer_pg_type(values) -> str:
    """Return ``BIGINT``, ``DOUBLE PRECISION`` or ``TEXT`` for an iterable of raw string values."""
    state = _ColTypeInference()
    for v in values:
        state.update(v)
    return state.resolve()


# Tables whose data rows are rotated against their own header. In hive_oecd_fsedata.csv
# the header lists 10 dimension columns and then 23 year columns, but each data row holds
# the 23 year values first and the 10 dimension values last. The value is the number of
# fields to rotate a row left by to match the header: 33 fields, so 23 left equals 10 right.
_ROTATED_TABLES: dict[str, int] = {"oecd.fsedata": 23}


def _realign_row(row: list[str], table: str) -> list[str]:
    """Rotate the fields of ``row`` into header order for a table in ``_ROTATED_TABLES``, and return other rows unchanged.

    ``tests/test_load_csv_row_realignment.py`` covers this.
    """
    n = _ROTATED_TABLES.get(table)
    if not n:
        return row
    return row[n:] + row[:n]


# Tables whose export mixes records with continuation lines. The fsedata export wrote
# each multi-line policy description as extra CSV lines, split at commas and padded to
# 33 fields. The first field of a record, after realignment, is an ISO3 country code.
_RECORD_KEY: dict[str, "re.Pattern[str]"] = {
    "oecd.fsedata": re.compile(r"^[A-Z]{3}$"),
}


def _is_record(row: list[str], table: str) -> bool:
    """Return False for a continuation fragment of ``table``, judging a row that is already realigned."""
    key = _RECORD_KEY.get(table)
    if key is None:
        return True
    return bool(row) and bool(key.match(row[0].strip()))


def _convert(value: str | None, pg_type: str):
    """Convert a raw CSV string to the Python type of the column, with ``None`` for empty and missing values."""
    if value is None or value == "":
        return None
    s = value.strip()
    if pg_type in ("BIGINT", "DOUBLE PRECISION"):
        if s.lower() in _NONFINITE:
            return None
        return int(s) if pg_type == "BIGINT" else float(s)
    return value


def _detect_encoding(path: str, chunk_size: int = 1 << 20) -> str:
    """Return ``utf-8`` when the whole file decodes as UTF-8, and ``latin-1`` otherwise.

    Latin-1 maps every byte to a character, so the fallback cannot fail to decode.
    """
    # Decode in chunks so a large file is not held in memory. The incremental decoder
    # carries a multi-byte character that is split at a chunk boundary into the next chunk.
    decoder = codecs.getincrementaldecoder("utf-8")()
    with open(path, "rb") as f:
        try:
            while chunk := f.read(chunk_size):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return "latin-1"
    return "utf-8"


def _infer_table_types(path: str, table: str = "") -> tuple[list[str], list[str]]:
    """Read the headers and infer a PostgreSQL type for each column, in one pass with constant memory.

    Inference uses the same realigned rows that `load_table` inserts, so ``table`` is looked up in ``_ROTATED_TABLES`` and ``_RECORD_KEY``.
    """
    with open(path, "r", encoding=_detect_encoding(path)) as f:
        reader = csv.reader(f)
        headers = next(reader)
        states = [_ColTypeInference() for _ in headers]
        for row in reader:
            row = _realign_row(row, table)
            if not _is_record(row, table):
                continue
            for i in range(len(headers)):
                if i < len(row):
                    states[i].update(row[i])
    return headers, [s.resolve() for s in states]


# Tables whose column types are taken from the declarations in `datasets/datasets.yaml`
# where the catalogue says ``string``. The catalogue declares ``string`` for some numeric
# columns of other tables, such as `year`, `value` and `revenue`, so applying the
# override to every table would turn those columns into TEXT. Add a table here only after
# checking that its ``string`` declarations are correct.
_DECLARED_TYPE_OVERRIDE_TABLES = frozenset({
    "fisheries.fisheries_landings",
    "fisheries.fisheries_landings_divisions",
})


def _declared_text_columns(table: str) -> set[str]:
    """Return the column names that `datasets/datasets.yaml` declares ``string`` for ``table``.

    The set is empty when ``table`` is not in ``_DECLARED_TYPE_OVERRIDE_TABLES`` or has no catalogue entry.
    For the two landings tables it keeps a column such as ``fao_area`` as TEXT even when the values in one file all look numeric.
    """
    if table not in _DECLARED_TYPE_OVERRIDE_TABLES:
        return set()

    from surimi_data_mcp.datasets.catalog import find_dataset

    ds = find_dataset(table)
    if ds is None:
        return set()
    return {c["name"] for c in ds["columns"] if c.get("type") == "string"}


def _override_declared_types(
    headers: list[str], types: list[str], table: str
) -> list[str]:
    """Return ``types`` with TEXT for every column that the catalogue declares ``string``.

    ``headers`` must already be sanitized, because the catalogue uses sanitized names. Columns with a numeric declaration keep the inferred type.
    """
    text_cols = _declared_text_columns(table)
    if not text_cols:
        return types
    return [
        "TEXT" if h in text_cols else t
        for h, t in zip(headers, types)
    ]


def _ensure_schemas(cur, tables) -> None:
    """Create each schema named in ``tables``, a list of ``schema.table`` strings, unless it already exists.

    It must run before any ``CREATE TABLE`` for those tables.
    """
    schemas = sorted({t.split(".", 1)[0] for t in tables})
    for schema in schemas:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def load_table(cur, path: str, table: str) -> tuple[int, int, int]:
    """Load one CSV into ``table`` and return (rows loaded, columns, numeric-typed columns).

    The rows go into ``<table>__loading``, and once all are in, one transaction drops the live table and renames the new one into its place. A reader therefore sees the old complete table and then the new complete one.
    The connection must be in autocommit mode.
    """
    headers, types = _infer_table_types(path, table)
    headers = _sanitize_headers(headers)
    types = _override_declared_types(headers, types, table)
    ncol = len(headers)
    encoding = _detect_encoding(path)

    staging = f"{table}__loading"
    cur.execute(f"DROP TABLE IF EXISTS {staging}")
    col_defs = ", ".join(f'"{h}" {t}' for h, t in zip(headers, types))
    cur.execute(f"CREATE TABLE {staging} ({col_defs})")

    cols = ", ".join(f'"{h}"' for h in headers)
    placeholders = ", ".join(["%s"] * ncol)
    insert_sql = f"INSERT INTO {staging} ({cols}) VALUES ({placeholders})"

    count = 0
    with open(path, "r", encoding=encoding) as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        batch = []
        for row in reader:
            row = _realign_row(row, table)
            if not _is_record(row, table):
                continue
            batch.append([
                _convert(row[i] if i < len(row) else None, types[i])
                for i in range(ncol)
            ])
            if len(batch) >= 10000:
                cur.executemany(insert_sql, batch)
                count += len(batch)
                batch = []
        if batch:
            cur.executemany(insert_sql, batch)
            count += len(batch)

    cur.execute("BEGIN")
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute(f"ALTER TABLE {staging} RENAME TO {table.split('.')[-1]}")
    cur.execute("COMMIT")
    typed = sum(1 for t in types if t != "TEXT")
    return count, ncol, typed


def main():
    """Load every file of `CSV_TO_TABLE` found in ``CSV_DIR`` (default ``data/exports``) into the database at ``DB_URL``."""
    import psycopg2

    export_dir = os.environ.get("CSV_DIR", "data/exports")
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()

    _ensure_schemas(cur, CSV_TO_TABLE.values())

    for csv_file, table in CSV_TO_TABLE.items():
        path = os.path.join(export_dir, csv_file)
        if not os.path.exists(path):
            print(f"SKIP {csv_file} (not found)")
            continue
        count, ncol, typed = load_table(cur, path, table)
        print(f"{table}: {count} rows loaded ({ncol} cols, {typed} typed numeric)")

    cur.close()
    conn.close()
    print("done")


if __name__ == "__main__":
    main()
