"""Incremental assembly of the versioned history of building objects from a
sequence of monthly snapshots (SCD2 - slowly changing dimension).

Principle:
    - object unchanged between snapshots -> no new row
    - object with something changed (an attribute or the geometry) -> the old
      version is closed (`valid_to` = snapshot date, `end_reason="change"`)
      and a new one is opened (`valid_from` = snapshot date, `valid_to=None`)
    - new object -> a new open version
    - object that disappeared from the snapshot -> its last version is closed
      (`end_reason="removed"`)
    - `valid_to IS NULL` == the object is valid as of the last processed snapshot
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .download import month_range
from .vfr_parser import CRS_VFR, VOLATILE_FIELDS

# control/metadata columns, not substantive attributes of a building object
_NON_ATTR_COLS = {
    "code", "gml_id", "geometry", "reference_point", "has_polygon",
    "snapshot_date", "source_file",
    "valid_from", "valid_to", "end_reason",
    *VOLATILE_FIELDS,
}


def _geoms_equal(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return a.equals(b)
    except Exception:
        return False


def _row_differs(old: dict, new: dict, attr_cols: list[str]) -> bool:
    for c in attr_cols:
        ov, nv = old.get(c), new.get(c)
        if pd.isna(ov) and pd.isna(nv):
            continue
        if ov != nv:
            return True
    return not _geoms_equal(old.get("geometry"), new.get("geometry"))


class HistoryState:
    """Holds the incrementally built versioned history of the building objects
    of one municipality."""

    def __init__(self):
        self.rows: dict[int, dict] = {}        # row id -> row data (open and closed alike)
        self.open_by_code: dict[str, int] = {}  # code -> id of the currently open row
        self._next_id = 0
        self.last_snapshot: pd.Timestamp | None = None
        self.processed_months: list[str] = []
        self.pending_months: list[str] = []
        self.skipped_months: list[str] = []
        self._attr_cols: list[str] | None = None

    # -- bookkeeping of processed / failed months --------------------------

    def _mark_processed(self, yyyymm: str | None) -> None:
        if not yyyymm:
            return
        if yyyymm not in self.processed_months:
            self.processed_months.append(yyyymm)
        if yyyymm in self.pending_months:
            self.pending_months.remove(yyyymm)

    def mark_failed(self, yyyymm: str, retry_from: str) -> bool:
        """Record a month whose snapshot could not be processed - be it because
        the source file is not on the server, or because it could not be
        downloaded or parsed. Returns True when the failure is taken as final."""
        if yyyymm in self.processed_months:
            return False
        if yyyymm >= retry_from:
            if yyyymm not in self.pending_months:
                self.pending_months.append(yyyymm)
            return False
        if yyyymm in self.pending_months:
            self.pending_months.remove(yyyymm)
        if yyyymm not in self.skipped_months:
            self.skipped_months.append(yyyymm)
        return True

    # -- range check before a run ------------------------------------------

    @property
    def last_snapshot_month(self) -> str | None:
        """The month (YYYYMM) the state is projected up to - i.e. the directory
        the last processed snapshot came from. Its `snapshot_date` (PlatiOd) is
        the day after, hence the subtracted day."""
        if self.last_snapshot is None:
            return None
        return (self.last_snapshot - pd.Timedelta(days=1)).strftime("%Y%m")

    def out_of_order_months(self, months: list[str]) -> list[str]:
        """Months from `months` that lie BEFORE the start of the already built
        series, and that a run can therefore no longer add."""
        done = set(self.processed_months)
        if not done:
            return []
        start = min(done)
        skipped = set(self.skipped_months)
        return [m for m in months if m < start and m not in skipped]

    def skipped_in(self, months: list[str]) -> list[str]:
        """Months from the given range that will not be tried again."""
        done = set(self.processed_months)
        skipped = set(self.skipped_months)
        return [m for m in months if m in skipped and m not in done]

    def gap_months(self, months: list[str]) -> list[str]:
        """The hole that processing the range `months` would NEWLY open in the
        series - i.e. the months between the end of the current series and the
        start of the given range."""
        if not months:
            return []
        last = self.last_snapshot_month
        if last is None or months[0] <= last:
            return []
        put_aside = set(self.skipped_months)
        return [m for m in month_range(last, months[0], clamp=False)[1:-1]
                if m not in put_aside]

    # -- ingest ------------------------------------------------------------

    def ingest(self, gdf: gpd.GeoDataFrame, yyyymm: str | None = None) -> None:
        """Fold one monthly snapshot into the history state. Calling it twice
        with the same month is safe and changes nothing."""
        if yyyymm is not None and yyyymm in self.processed_months:
            return

        if len(gdf) == 0:
            self._mark_processed(yyyymm)
            return

        snapshot_date = gdf["snapshot_date"].iloc[0]
        if self.last_snapshot is not None and snapshot_date <= self.last_snapshot:
            self._mark_processed(yyyymm)
            return

        if self._attr_cols is None:
            self._attr_cols = [c for c in gdf.columns if c not in _NON_ATTR_COLS]
        attr_cols = self._attr_cols

        new_by_code = {row["code"]: row for row in gdf.to_dict("records")}

        # vanished objects -> close as removed
        for code in list(self.open_by_code):
            if code not in new_by_code:
                self._close(code, snapshot_date, "removed")

        # new / changed / unchanged
        for code, new_row in new_by_code.items():
            if code not in self.open_by_code:
                self._open(new_row, snapshot_date)
                continue

            row_id = self.open_by_code[code]
            old_row = self.rows[row_id]
            if _row_differs(old_row, new_row, attr_cols):
                self._close(code, snapshot_date, "change")
                self._open(new_row, snapshot_date)

        self.last_snapshot = snapshot_date
        self._mark_processed(yyyymm)

    def _open(self, row: dict, snapshot_date) -> None:
        row = dict(row)
        row["valid_from"] = snapshot_date
        row["valid_to"] = pd.NaT
        row["end_reason"] = None
        row_id = self._next_id
        self._next_id += 1
        self.rows[row_id] = row
        self.open_by_code[row["code"]] = row_id

    def _close(self, code: str, snapshot_date, reason: str) -> None:
        row_id = self.open_by_code.pop(code)
        self.rows[row_id]["valid_to"] = snapshot_date
        self.rows[row_id]["end_reason"] = reason

    # -- export / persist ----------------------------------------------------

    def to_geodataframe(self) -> gpd.GeoDataFrame:
        if not self.rows:
            return gpd.GeoDataFrame(columns=["code", "valid_from", "valid_to", "end_reason", "geometry"],
                                     geometry="geometry", crs=CRS_VFR)
        df = pd.DataFrame(list(self.rows.values()))
        gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=CRS_VFR)
        if "reference_point" in gdf.columns:
            gdf["reference_point"] = gpd.GeoSeries(gdf["reference_point"], crs=CRS_VFR)
        return gdf.sort_values(["code", "valid_from"]).reset_index(drop=True)

    def save(self, path: str | Path) -> None:
        """Save the state into GeoParquet, with a little progress metadata next
        to it. The `.parquet` is always written before the `.progress.json`, so
        the metadata can only ever lag the data, never lead it."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        gdf = self.to_geodataframe()

        tmp_path = path.with_suffix(path.suffix + ".tmp")
        gdf.to_parquet(tmp_path)
        os.replace(tmp_path, path)

        meta_path = path.with_suffix(".progress.json")
        meta_tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
        meta_tmp.write_text(
            json.dumps({
                "processed_months": self.processed_months,
                "pending_months": self.pending_months,
                "skipped_months": self.skipped_months,
                "last_snapshot": str(self.last_snapshot) if self.last_snapshot is not None else None,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(meta_tmp, meta_path)

    @classmethod
    def load_progress(cls, path: str | Path) -> "HistoryState":
        """Load ONLY the progress metadata (`.progress.json`), without the data
        from the `.parquet`. The returned state has no history rows, so it can
        answer the range checks but must never be fed to `ingest()`."""
        path = Path(path)
        state = cls()
        meta_path = path.with_suffix(".progress.json")
        if not meta_path.exists():
            return state
        state._read_meta(json.loads(meta_path.read_text(encoding="utf-8")))
        return state

    @classmethod
    def load(cls, path: str | Path) -> "HistoryState":
        path = Path(path)
        state = cls()
        if not path.exists():
            return state

        gdf = gpd.read_parquet(path)
        state._attr_cols = [c for c in gdf.columns if c not in _NON_ATTR_COLS]
        for row_id, row in enumerate(gdf.to_dict("records")):
            state.rows[row_id] = row
            state._next_id = row_id + 1
            if pd.isna(row.get("valid_to")):
                state.open_by_code[row["code"]] = row_id

        meta_path = path.with_suffix(".progress.json")
        if meta_path.exists():
            state._read_meta(json.loads(meta_path.read_text(encoding="utf-8")))

        if state.rows:
            seen_dates = [r["valid_from"] for r in state.rows.values() if pd.notna(r.get("valid_from"))]
            seen_dates += [r["valid_to"] for r in state.rows.values() if pd.notna(r.get("valid_to"))]
            if seen_dates:
                data_last_snapshot = max(seen_dates)
                if state.last_snapshot is None or data_last_snapshot > state.last_snapshot:
                    state.last_snapshot = data_last_snapshot

        return state

    def _read_meta(self, meta: dict) -> None:
        """Transfer the contents of `.progress.json` into the state."""
        self.processed_months = meta.get("processed_months", [])
        self.pending_months = meta.get("pending_months", [])
        self.skipped_months = meta.get("skipped_months", [])
        last = meta.get("last_snapshot")
        self.last_snapshot = pd.to_datetime(last) if last else None
