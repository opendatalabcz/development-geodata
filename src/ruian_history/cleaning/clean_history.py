"""Clean one `<name>_history.parquet` produced by
`ruian_history/pipeline/build_region.py`, per the rules in the `README.md` next to this script.

The cleaned GeoParquet goes to `data/clean_output/`; the log of every
drop/null/fill goes to `data/cleaning_logs/<name>_cleaning_log.csv`
(the data directory is `./data`, or `$RUIAN_DATA_DIR`).

Usage:
    python -m ruian_history.cleaning.clean_history 539309
    python -m ruian_history.cleaning.clean_history brno_area --input-dir data/output
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from ..paths import CLEAN_OUTPUT_DIR, CLEANING_LOG_DIR, OUTPUT_DIR
from ..pipeline.export_parquet import write_geoparquet

CODELIST_DIR = Path(__file__).resolve().parent / "codelists"

# column -> codelist file in CODELIST_DIR (KOD;NAZEV;..., cp1250, ';'-separated)
CODELISTS = {
    "building_type_code": "CS_TYP_STAVEBNIHO_OBJEKTU.csv",
    "usage_type_code": "CE_ZPUSOB_VYUZITI_OBJEKTU.csv",
    "construction_type_code": "CE_DRUH_KONSTRUKCE.csv",
    "sewage_connection_code": "CE_PRIPOJENI_KANAL.csv",
    "gas_connection_code": "CE_PRIPOJENI_PLYNU.csv",
    "water_connection_code": "CE_PRIPOJENI_VODY.csv",
    "elevator_code": "CE_VYBAVENI_VYTAHEM.csv",
    "heating_type_code": "CE_ZPUSOB_VYTAPENI.csv",
}

# column -> (min, max), inclusive
NUMERIC_RANGES = {
    "unit_count": (0, 1000),
    "floor_count": (0, 40),
    "built_up_area": (0, 100_000),
}

MIN_COMPLETION_YEAR = 1000

# geometry stays in S-JTSK (EPSG:5514); `<col>_wgs84` is an added copy for web maps
GEOMETRY_COLS = ["geometry", "reference_point"]
WGS84_EPSG = 4326

REQUIRED_COLS = [
    "gml_id", "municipality_code", "municipality_name", "snapshot_date", "source_file",
    "valid_from", "change_proposal_global_id", "transaction_id",
]

DATE_COLS = ["record_valid_from", "completion_date", "snapshot_date", "valid_from", "valid_to"]

LOG_COLS = ["code", "row_index", "column", "old_value", "new_value", "action", "reason"]


class CleaningLog:
    """Accumulates one record per row/value touched during cleaning, for the
    `<name>_cleaning_log.csv` report."""

    def __init__(self):
        self._rows: list[dict] = []

    def add(self, code, row_index, column, old_value, new_value, action, reason):
        """Append one record to the log."""
        self._rows.append({
            "code": code, "row_index": row_index, "column": column,
            "old_value": old_value, "new_value": new_value,
            "action": action, "reason": reason,
        })

    def to_frame(self) -> pd.DataFrame:
        """Return the log as a DataFrame with `LOG_COLS` columns."""
        log = pd.DataFrame(self._rows, columns=LOG_COLS)
        # old/new values mix strings, numbers and dates, so they are stored as text
        for col in ("old_value", "new_value"):
            log[col] = log[col].map(lambda v: None if v is None or v is pd.NA or v != v else str(v))
        log["code"] = log["code"].astype(str)
        return log


def _null_and_log(df: pd.DataFrame, mask: pd.Series, col: str, log: CleaningLog, reason: str) -> pd.DataFrame:
    """Return `df` with `col` set to missing where `mask` is true, logging each change."""
    for idx in df.index[mask]:
        log.add(df.at[idx, "code"], idx, col, df.at[idx, col], None, "nulled_value", reason)
    values = df[col]
    if pd.api.types.is_integer_dtype(values) and not pd.api.types.is_extension_array_dtype(values):
        # nulling must produce missing, not a 0 that reads back as a real recorded value
        values = values.astype("float64")
    return df.assign(**{col: values.mask(mask)})


def load_history(path: Path) -> gpd.GeoDataFrame:
    """Read the history GeoParquet and parse its date columns."""
    gdf = gpd.read_parquet(path)
    for col in DATE_COLS:
        if col in gdf.columns:
            gdf[col] = pd.to_datetime(gdf[col], errors="coerce")
    return gdf


def load_codelists() -> dict[str, set | None]:
    """Load the valid codes for each codelist-checked column; `None` where the codelist file is missing."""
    codelists: dict[str, set | None] = {}
    for col, fname in CODELISTS.items():
        path = CODELIST_DIR / fname
        if not path.exists():
            print(f"warning: codelist {fname} not found, skipping the {col} check")
            codelists[col] = None
            continue
        cl = pd.read_csv(path, sep=";", encoding="cp1250")
        codelists[col] = set(cl["KOD"].astype(int))
    return codelists


def check_scd2_invariants(df: pd.DataFrame) -> None:
    """The valid_from/valid_to/end_reason bookkeeping is derived by the
    pipeline's own SCD2 build. A violation here means
    a pipeline bug, so cleaning refuses to run."""
    problems = []

    if df["valid_from"].isna().any():
        problems.append(f"{df['valid_from'].isna().sum()} rows with a missing valid_from")

    open_mismatch = df["valid_to"].isna() != df["end_reason"].isna()
    if open_mismatch.any():
        problems.append(f"{open_mismatch.sum()} rows where valid_to and end_reason disagree "
                         f"on whether the version is open")

    closed = df["valid_to"].notna()
    backwards = closed & (df["valid_from"] >= df["valid_to"])
    if backwards.any():
        problems.append(f"{backwards.sum()} rows with valid_from >= valid_to")

    if "snapshot_date" in df.columns:
        mismatched = df["snapshot_date"] != df["valid_from"]
        if mismatched.any():
            problems.append(f"{mismatched.sum()} rows where snapshot_date != valid_from")

    future_record = df["record_valid_from"] > df["valid_from"]
    if future_record.any():
        problems.append(f"{future_record.sum()} rows where record_valid_from is later than valid_from")

    dup = df.duplicated(subset=["code", "valid_from"])
    if dup.any():
        problems.append(f"{dup.sum()} duplicate (code, valid_from) pairs")

    multi_open = df[df["valid_to"].isna()].duplicated(subset=["code"])
    if multi_open.any():
        problems.append(f"{multi_open.sum()} objects with more than one open version")

    if problems:
        raise SystemExit(
            "Refusing to clean: the input violates SCD2 invariants that the pipeline "
            "itself should already guarantee:\n" + "\n".join(f"  - {p}" for p in problems)
        )


def drop_invalid_identifiers(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    """Drop rows with a missing required column or a `code` that is not a positive integer."""
    bad = pd.Series(False, index=df.index)

    for col in REQUIRED_COLS:
        missing = df[col].isna()
        for idx in df.index[missing]:
            log.add(df.at[idx, "code"], idx, col, None, None, "dropped_row",
                     f"required column '{col}' is missing")
        bad |= missing

    code_num = pd.to_numeric(df["code"], errors="coerce")
    invalid_code = code_num.isna() | (code_num <= 0) | (code_num % 1 != 0)
    for idx in df.index[invalid_code & ~bad]:
        log.add(df.at[idx, "code"], idx, "code", df.at[idx, "code"], None, "dropped_row",
                 "code is not a positive integer")
    bad |= invalid_code

    return df[~bad].reset_index(drop=True)


def null_out_of_codelist(df: pd.DataFrame, codelists: dict[str, set | None], log: CleaningLog) -> pd.DataFrame:
    """Return `df` with values that are not in the column's codelist nulled."""
    for col, valid_codes in codelists.items():
        if col not in df.columns or valid_codes is None:
            continue
        present = df[col].notna()
        as_int = pd.to_numeric(df.loc[present, col], errors="coerce")
        bad_idx = as_int.index[as_int.isna() | ~as_int.isin(valid_codes)]
        mask = df.index.isin(bad_idx)
        df = _null_and_log(df, mask, col, log, f"value outside the {col} codelist")
    return df


def null_out_of_range(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    """Return `df` with numeric values outside their plausible range, and a `district_code` that is not a positive integer, nulled."""
    for col, (lo, hi) in NUMERIC_RANGES.items():
        if col not in df.columns:
            continue
        bad = df[col].notna() & ((df[col] < lo) | (df[col] > hi))
        df = _null_and_log(df, bad, col, log, f"outside the plausible range [{lo}, {hi}]")

    if "district_code" in df.columns:
        num = pd.to_numeric(df["district_code"], errors="coerce")
        bad = df["district_code"].notna() & ~((num > 0) & (num % 1 == 0))
        df = _null_and_log(df, bad, "district_code", log, "not a positive integer")
    return df


def null_implausible_completion_dates(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    """Return `df` with `completion_date` before year `MIN_COMPLETION_YEAR` or after the row's snapshot date nulled."""
    col = "completion_date"
    # "future" is judged against the snapshot the row comes from (valid_from == snapshot_date)
    bad = df[col].notna() & ((df[col].dt.year < MIN_COMPLETION_YEAR) | (df[col] > df["valid_from"]))
    return _null_and_log(df, bad, col, log, f"implausible date (before year {MIN_COMPLETION_YEAR} or after the row's snapshot date)")


def fill_completion_date_across_versions(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    """Return `df` with missing completion_date values filled from other versions of the same object; the rules are in the README."""
    col = "completion_date"
    ordered = df.assign(_code=pd.to_numeric(df["code"])).sort_values(["_code", "valid_from"])
    by_object = ordered.groupby("_code")[col]
    candidate = by_object.ffill().fillna(by_object.bfill())

    missing = ordered[col].isna() & candidate.notna()
    fits = candidate <= ordered["valid_from"]
    for idx in ordered.index[missing & fits]:
        log.add(ordered.at[idx, "_code"], idx, col, None, candidate[idx], "filled_value",
                "completion_date propagated from the nearest known value for this object")
    for idx in ordered.index[missing & ~fits]:
        log.add(ordered.at[idx, "_code"], idx, col, None, None, "left_null",
                "the nearest known completion_date for this object is later than this version's valid_from")

    return df.assign(**{col: df[col].where(~(missing & fits), candidate)})


def clean_history(in_path: Path, out_path: Path, log_path: Path) -> None:
    """Run all cleaning steps on one history file and write the cleaned GeoParquet and the log CSV."""
    df = load_history(in_path)
    check_scd2_invariants(df)

    log = CleaningLog()
    df = drop_invalid_identifiers(df, log)

    df = null_out_of_codelist(df, load_codelists(), log)
    df = null_out_of_range(df, log)
    df = null_implausible_completion_dates(df, log)
    df = fill_completion_date_across_versions(df, log)

    for col in GEOMETRY_COLS:
        df[f"{col}_wgs84"] = df[col].to_crs(WGS84_EPSG)

    write_geoparquet(df, out_path)

    log_df = log.to_frame()
    log_df.to_csv(log_path, index=False, encoding="utf-8-sig")
    by_action = ", ".join(f"{n} {a}" for a, n in log_df["action"].value_counts().items())
    print(f"Saved: {log_path} ({len(log_df)} entries: {by_action})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean a pipeline history GeoParquet.")
    parser.add_argument("name", help="base name of the pipeline output, e.g. 539309 or brno_area")
    parser.add_argument("--input-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=CLEAN_OUTPUT_DIR)
    parser.add_argument("--log-dir", type=Path, default=CLEANING_LOG_DIR)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    clean_history(
        args.input_dir / f"{args.name}_history.parquet",
        args.output_dir / f"{args.name}_history_clean.parquet",
        args.log_dir / f"{args.name}_cleaning_log.csv",
    )


if __name__ == "__main__":
    main()
