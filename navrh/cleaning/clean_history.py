"""Clean one `<name>_history.parquet` produced by
`navrh/pipeline/build_region.py`, per the rules in `plan_cisteni_dat.md`
(next to this script):

    A. row missing an identifying/structural column -> row dropped
    B. categorical code outside its RÚIAN codelist -> value nulled
    C/D. numeric or date attribute outside a plausible range -> value nulled
    E. optional descriptive attributes -> left untouched, missing is fine
    F. invalid geometry -> repaired if possible, else nulled
    G. SCD2 invariants (valid_from/valid_to/end_reason, ...) -> only asserted,
       never silently fixed; a violation aborts the run

The cleaned GeoParquet goes to `navrh/clean_output/`; the log of every
drop/null/fill goes to `navrh/cleaning/logs/<name>_cleaning_log.parquet`.

Usage:
    python -m navrh.cleaning.clean_history 539309
    python -m navrh.cleaning.clean_history brno_area --input-dir navrh/output
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.validation import make_valid

from navrh.pipeline.export_parquet import write_geoparquet

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
OUT_DIR = REPO_ROOT / "navrh" / "output"
CLEAN_OUTPUT_DIR = REPO_ROOT / "navrh" / "clean_output"
LOG_DIR = SCRIPT_DIR / "logs"
CODELIST_DIR = REPO_ROOT / "analyza" / "ciselniky"

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

REQUIRED_COLS = [
    "gml_id", "municipality_code", "municipality_name", "snapshot_date", "source_file",
    "valid_from", "change_proposal_global_id", "transaction_id",
]

DATE_COLS = ["record_valid_from", "completion_date", "snapshot_date", "valid_from", "valid_to"]

LOG_COLS = ["code", "row_index", "column", "old_value", "new_value", "action", "reason"]


class CleaningLog:
    """Accumulates one record per row/value touched during cleaning, for the
    `<name>_cleaning_log.parquet` report."""

    def __init__(self):
        self._rows: list[dict] = []

    def add(self, code, row_index, column, old_value, new_value, action, reason):
        self._rows.append({
            "code": code, "row_index": row_index, "column": column,
            "old_value": old_value, "new_value": new_value,
            "action": action, "reason": reason,
        })

    def to_frame(self) -> pd.DataFrame:
        log = pd.DataFrame(self._rows, columns=LOG_COLS)
        # old/new values mix strings, numbers and dates, so they are stored as text
        for col in ("old_value", "new_value"):
            log[col] = log[col].map(lambda v: None if v is None or v is pd.NA or v != v else str(v))
        log["code"] = log["code"].astype(str)
        return log


def _null_and_log(df: pd.DataFrame, mask: pd.Series, col: str, log: CleaningLog, reason: str) -> None:
    for idx in df.index[mask]:
        log.add(df.at[idx, "code"], idx, col, df.at[idx, col], None, "nulled_value", reason)
    if pd.api.types.is_integer_dtype(df[col]) and not pd.api.types.is_extension_array_dtype(df[col]):
        # nulling must produce missing, not a 0 that reads back as a real recorded value
        df[col] = df[col].astype("float64")
    df.loc[mask, col] = np.nan


def load_history(path: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_parquet(path)
    for col in DATE_COLS:
        if col in gdf.columns:
            gdf[col] = pd.to_datetime(gdf[col], errors="coerce")
    return gdf


def load_codelists() -> dict[str, set | None]:
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
    pipeline's own SCD2 build, not sourced from RÚIAN - a violation here means
    a pipeline bug, so cleaning refuses to run rather than paper over it."""
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


def null_out_of_codelist(df: pd.DataFrame, codelists: dict[str, set | None], log: CleaningLog) -> None:
    for col, valid_codes in codelists.items():
        if col not in df.columns or valid_codes is None:
            continue
        present = df[col].notna()
        as_int = pd.to_numeric(df.loc[present, col], errors="coerce")
        bad_idx = as_int.index[as_int.isna() | ~as_int.isin(valid_codes)]
        mask = df.index.isin(bad_idx)
        _null_and_log(df, mask, col, log, f"value outside the {col} codelist")


def null_out_of_range(df: pd.DataFrame, log: CleaningLog) -> None:
    for col, (lo, hi) in NUMERIC_RANGES.items():
        if col not in df.columns:
            continue
        bad = df[col].notna() & ((df[col] < lo) | (df[col] > hi))
        _null_and_log(df, bad, col, log, f"outside the plausible range [{lo}, {hi}]")

    if "district_code" in df.columns:
        bad = df["district_code"].notna() & (pd.to_numeric(df["district_code"], errors="coerce") <= 0)
        _null_and_log(df, bad, "district_code", log, "not a positive number")


def null_implausible_completion_dates(df: pd.DataFrame, log: CleaningLog) -> None:
    col = "completion_date"
    today = pd.Timestamp.today().normalize()
    bad = df[col].notna() & ((df[col].dt.year < MIN_COMPLETION_YEAR) | (df[col] > today))
    _null_and_log(df, bad, col, log, f"implausible date (before year {MIN_COMPLETION_YEAR} or in the future)")


def fill_completion_date_across_versions(df: pd.DataFrame, log: CleaningLog) -> None:
    """completion_date describes the object, not the version: a still-null
    version is filled from the nearest earlier version of the same `code`
    that already has a value, falling back to the nearest later version only
    when no earlier one exists - this also covers a retrospective correction
    (a later version recording an earlier date than one already known), which
    then applies only from that later version onward. A version keeps its own
    recorded value even when a later correction suggests it was wrong, and a
    value is never propagated into a version it would place after that
    version's own valid_from - the object was evidently still under
    construction then."""
    col = "completion_date"
    codes = pd.to_numeric(df["code"]).to_numpy()
    valid_from = df["valid_from"].to_numpy()
    completion = df[col].to_numpy(copy=True)

    order = np.lexsort((valid_from, codes))
    group_start = np.flatnonzero(np.r_[True, codes[order][1:] != codes[order][:-1]])
    group_end = np.r_[group_start[1:], len(order)]

    for start, end in zip(group_start, group_end):
        positions = order[start:end]  # this object's row positions, chronological (valid_from) order
        group_completion = completion[positions]
        known = np.flatnonzero(~pd.isna(group_completion))
        if len(known) == 0:
            continue
        known_values = group_completion[known]

        for local_i in np.flatnonzero(pd.isna(group_completion)):
            vf_i = valid_from[positions[local_i]]
            split = np.searchsorted(known, local_i)  # known[:split] earlier versions, known[split:] later ones

            source = next((k for k in range(split - 1, -1, -1) if known_values[k] <= vf_i), None)
            if source is None:
                source = next((k for k in range(split, len(known)) if known_values[k] <= vf_i), None)

            pos = positions[local_i]
            if source is None:
                log.add(codes[pos], pos, col, None, None, "left_null",
                         "no known completion_date for this object is <= this version's valid_from")
                continue
            value = known_values[source]
            completion[pos] = value
            log.add(codes[pos], pos, col, None, value, "filled_value",
                     "completion_date propagated from the nearest known value for this object")

    df[col] = completion


def repair_or_null_geometry(gdf: gpd.GeoDataFrame, log: CleaningLog) -> gpd.GeoDataFrame:
    bad = gdf.geometry.notna() & ~gdf.geometry.is_valid
    for idx in gdf.index[bad]:
        geom = gdf.at[idx, "geometry"]
        repaired = make_valid(geom)
        if repaired.is_valid and not repaired.is_empty:
            gdf.at[idx, "geometry"] = repaired
            log.add(gdf.at[idx, "code"], idx, "geometry", geom.wkt, repaired.wkt, "repaired_geometry",
                     "invalid geometry (self-intersection or similar), repaired")
        else:
            gdf.at[idx, "geometry"] = None
            log.add(gdf.at[idx, "code"], idx, "geometry", geom.wkt, None, "nulled_value",
                     "invalid geometry, could not be repaired")
    return gdf


def clean_history(in_path: Path, out_path: Path, log_path: Path) -> None:
    df = load_history(in_path)
    check_scd2_invariants(df)

    log = CleaningLog()
    df = drop_invalid_identifiers(df, log)

    null_out_of_codelist(df, load_codelists(), log)
    null_out_of_range(df, log)
    null_implausible_completion_dates(df, log)
    fill_completion_date_across_versions(df, log)

    df = repair_or_null_geometry(df, log)

    write_geoparquet(df, out_path)

    log_df = log.to_frame()
    log_df.to_parquet(log_path, index=False)
    by_action = ", ".join(f"{n} {a}" for a, n in log_df["action"].value_counts().items())
    print(f"Saved: {log_path} ({len(log_df)} entries: {by_action})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean a pipeline history GeoParquet.")
    parser.add_argument("name", help="base name of the pipeline output, e.g. 539309 or brno_area")
    parser.add_argument("--input-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=CLEAN_OUTPUT_DIR)
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    clean_history(
        args.input_dir / f"{args.name}_history.parquet",
        args.output_dir / f"{args.name}_history_clean.parquet",
        args.log_dir / f"{args.name}_cleaning_log.parquet",
    )


if __name__ == "__main__":
    main()
