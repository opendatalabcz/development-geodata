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

The state is kept in memory (for a single municipality that means hundreds to
thousands of objects, so it is not a problem) and can be saved/loaded at any
time via GeoParquet - see `HistoryState.save` / `HistoryState.load` - so the
processing can be interrupted and resumed at any point.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .download import month_range
from .vfr_parser import CRS_VFR, VOLATILE_FIELDS

# columns that are not a "substantive attribute" of a building object
# (control/metadata columns)
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
        self._legacy_meta: str | None = None
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
        downloaded or parsed. Returns True when the failure is taken as final.

        Only one thing decides that: whether the file can still show up on the
        server. ČÚZK publishes months in order and does not backfill older
        ones, so a failure for a month older than `retry_from` (= the last
        published month, see `download.latest_available_month`) is final.
        Retrying it next time cannot bring anything - it cannot be filled in
        anyway, because the diffing in `ingest()` depends on the order - and it
        only costs requests. Such a month goes into `skipped_months` and
        `build_region.py` does not try it a second time.

        A month from `retry_from` onwards, on the other hand, may still appear:
        typically the end of the range, which ČÚZK had not published at the time
        of the run. That one goes into `pending_months` and the next run tries
        it again.

        A caller that does not know the real last published month (the /vfr/
        listing could not be loaded) passes `retry_from` at the start of the
        range - then nothing is declared final and everything is retried. That
        direction is the safe one: a needless attempt costs one request, whereas
        a wrongly discarded month would be missing from the history for good."""
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
        the last processed snapshot came from.

        The file `YYYYMMDD` lies in the directory of its own month, but its
        `snapshot_date` (PlatiOd) is the following day - so for a snapshot from
        the end of a month `last_snapshot` already falls into the next month.
        Hence the subtracted day, see `vfr_parser`."""
        if self.last_snapshot is None:
            return None
        return (self.last_snapshot - pd.Timedelta(days=1)).strftime("%Y%m")

    def out_of_order_months(self, months: list[str]) -> list[str]:
        """Months from `months` that lie BEFORE the start of the already built
        series.

        The diffing in `ingest()` is inherently order-dependent, so such a
        snapshot cannot be added to the state after the fact - `ingest()` would
        drop it (see the `snapshot_date <= self.last_snapshot` check). That used
        to happen silently and the resulting history was quietly incomplete; the
        caller (`build_region.py`) therefore checks this list up front and
        rather stops the run than discards data without a word.

        The decision is made against the *start* of the series, not its end: a
        month missing inside an already processed series is unreachable too, but
        that is not an error in the given range - the run already went through
        it once and it did not work out, see `mark_failed()` / `skipped_in()`.
        Were this blocked as well, a single permanently broken item on the
        server would kill that municipality forever.

        For the same reason months already set aside (`skipped_months`) do not
        count here: for those the situation is clear - the run tried them, the
        file does not exist and never will, so nothing is silently discarded and
        the user cannot do anything about it anyway. Typically it is a
        municipality that did not exist yet at the start of the archive; without
        this exception the default range (from `ARCHIVE_START`) would report an
        error for it on every further run and the regular topping up of new
        months would not work for it at all."""
        done = set(self.processed_months)
        if not done:
            return []
        start = min(done)
        skipped = set(self.skipped_months)
        return [m for m in months if m < start and m not in skipped]

    def skipped_in(self, months: list[str]) -> list[str]:
        """Months from the given range that will not be tried again.

        These are the ones whose source file could not be processed once and for
        which no new one can appear on the server (see `mark_failed`). They
        cannot be filled in retroactively (see `out_of_order_months`), so all
        that is left is to report them - so that the caller knows where a hole
        remains in the history and can reach for a rebuild from scratch."""
        done = set(self.processed_months)
        skipped = set(self.skipped_months)
        return [m for m in months if m in skipped and m not in done]

    def gap_months(self, months: list[str]) -> list[str]:
        """The hole that processing the range `months` would NEWLY open in the
        series - i.e. the months between the end of the current series and the
        start of the given range.

        A hole in the series is not an error of the run, but it distorts the
        result: an object that disappeared inside the hole gets its `valid_to`
        only from the first snapshot past the hole. It is therefore reported up
        front, while something can still be done about it.

        Holes that are already in the state do not count here - this run did not
        cause them and they cannot be filled in anyway (see `skipped_in`).
        Neither do months already put aside (`skipped_months`): the caller has
        no way to get those, so reporting them as something to fix would stop a
        run over a hole nobody can close. What is left here is therefore always
        actionable - months that are on the server and that the range merely
        skipped over."""
        if not months:
            return []
        last = self.last_snapshot_month
        if last is None or months[0] <= last:
            # a clean state, or the range continues/overlaps the current series;
            # `months` itself always comes from `month_range()` and is contiguous
            return []
        put_aside = set(self.skipped_months)
        return [m for m in month_range(last, months[0], clamp=False)[1:-1]
                if m not in put_aside]

    # -- ingest ------------------------------------------------------------

    def ingest(self, gdf: gpd.GeoDataFrame, yyyymm: str | None = None) -> None:
        """Fold one monthly snapshot into the history state.

        It has to be safe to call twice in a row with the same month without
        changing the result (idempotent) - after a process crash and a resume
        (see `build_region.py`) it can happen that `.progress.json` does not
        match exactly what is really stored in the `.parquet` (`save()` writes
        the two as separate, non-atomic writes), so the same month may be
        attempted a second time."""
        if yyyymm is not None and yyyymm in self.processed_months:
            return

        if len(gdf) == 0:
            self._mark_processed(yyyymm)
            return

        snapshot_date = gdf["snapshot_date"].iloc[0]
        if self.last_snapshot is not None and snapshot_date <= self.last_snapshot:
            # the same guard by snapshot date, in case `processed_months` from an
            # older/inconsistent state did not contain this month even though the
            # data from it had already been folded in
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
            # otherwise unchanged -> nothing happens

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
            # the second (secondary) geometry column has to be a real GeoSeries
            # too, otherwise the Parquet writer cannot serialize it
            gdf["reference_point"] = gpd.GeoSeries(gdf["reference_point"], crs=CRS_VFR)
        return gdf.sort_values(["code", "valid_from"]).reset_index(drop=True)

    def save(self, path: str | Path) -> None:
        """Save the state into GeoParquet (+ a little progress metadata next to it).

        Both files are written atomically (via a temporary file + `os.replace`,
        which is an atomic rename on the same volume) - so a half-written or
        truncated file cannot appear even if the process crashes mid-write. On
        top of that the `.parquet` is always written first and the
        `.progress.json` only after it, so even on a crash exactly between the
        two writes the `.progress.json` stays at most behind the `.parquet`
        (never ahead) - and that is something `ingest()` can safely catch up
        with (see there)."""
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
        from the `.parquet`.

        For the range check before a run (see `build_region.py`) it is enough to
        know which months are done and how far the state is projected; loading
        the whole GeoParquet for that (hundreds of thousands of rows for a big
        city, and that for every municipality of the region) would be wasted
        work.

        The returned state does NOT contain the history rows - it is usable only
        for `processed_months` / `skipped_in()` / `out_of_order_months()` /
        `gap_months()`, not for `ingest()`. Compared to `load()` it also lacks
        the derivation of `last_snapshot` from the data, so after a crash exactly
        between writing the `.parquet` and the `.progress.json` the
        `last_snapshot` may be a month behind; for the check that is the safe
        direction (at worst it does not warn about a month `ingest()` would drop
        anyway)."""
        path = Path(path)
        state = cls()
        meta_path = path.with_suffix(".progress.json")
        if not meta_path.exists():
            return state
        state._read_meta(json.loads(meta_path.read_text(encoding="utf-8")))
        # the same conversion of old states as in `load()` - otherwise the range
        # check would see a different progress than the worker later will
        state._migrate_legacy_progress()
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

        # a guard against the `.progress.json` being older than the `.parquet`
        # even after an atomic write (see `save()`) - what is visible directly in
        # the data (valid_from/valid_to of already processed rows) is taken as
        # the truth, so that `ingest()` does not reopen months that are already
        # in the data
        if state.rows:
            seen_dates = [r["valid_from"] for r in state.rows.values() if pd.notna(r.get("valid_from"))]
            seen_dates += [r["valid_to"] for r in state.rows.values() if pd.notna(r.get("valid_to"))]
            if seen_dates:
                data_last_snapshot = max(seen_dates)
                if state.last_snapshot is None or data_last_snapshot > state.last_snapshot:
                    state.last_snapshot = data_last_snapshot

        # only after deriving `last_snapshot` from the data - the conversion of
        # old states relies on it
        state._migrate_legacy_progress()

        return state

    def _read_meta(self, meta: dict) -> None:
        """Transfer the contents of `.progress.json` into the state.

        The metadata format changed over time, so it is detected right away
        whether this is an older write, and noted for
        `_migrate_legacy_progress()`."""
        self.processed_months = meta.get("processed_months", [])
        self.pending_months = meta.get("pending_months", meta.get("missing_months", []))
        self.skipped_months = meta.get("skipped_months", [])
        last = meta.get("last_snapshot")
        self.last_snapshot = pd.to_datetime(last) if last else None
        if "skipped_months" in meta:
            self._legacy_meta = None
        elif "missing_months" in meta:
            self._legacy_meta = "no_skipped_months"
        else:
            self._legacy_meta = "no_missing_months"

    def _migrate_legacy_progress(self) -> None:
        """Fill in what is missing from the metadata of states saved by older
        versions, so that only the current format is worked with further on."""
        kind = self._legacy_meta
        self._legacy_meta = None
        if kind is None:
            return
        if kind == "no_missing_months":
            self._migrate_legacy_missing_months()
        self._derive_skipped_months()

    def _derive_skipped_months(self) -> None:
        """Derive `skipped_months` for a state that did not track them yet.

        The same thing used to be derived from the position on every run anew: a
        month that lies inside an already built series and is not processed was
        tried once by a run and did not work out. It cannot be filled in anyway,
        so that is exactly `skipped_months` - only now it is written down once
        instead of being recomputed every time.

        Months *before* the start of the series are not put here: the state says
        nothing about those (the municipality may not have been in RÚIAN at the
        time at all, or they have not been touched yet). They are therefore tried
        once more and `mark_failed()` then records them itself - this is exactly
        what the old derivation logic could not do and why it kept downloading
        such months over and over."""
        last = self.last_snapshot_month
        done = set(self.processed_months)
        if last is None or not done or min(done) > last:
            return
        inside = month_range(min(done), last, clamp=False)
        self.skipped_months = sorted(set(self.skipped_months) | (set(inside) - done))

    def _migrate_legacy_missing_months(self) -> None:
        """A fix for states saved by an older version that wrote missing months
        straight into `processed_months` (and thereby killed them for good).

        Telling "processed, nothing changed" apart from "the file was not there"
        retroactively is not possible - both leave the same trace in
        `processed_months`. We do have certainty for the months *past* the last
        genuinely processed snapshot though: `ingest()` demonstrably never got to
        those (otherwise `last_snapshot` would be further along), so those were
        missing files. And those are exactly the problematic case - the end of
        the range that ČÚZK had not published yet at the time of the run.
        Removing them from `processed_months` makes the next run try them again
        (see `mark_failed`).

        Any missing months inside the series stay marked as processed; ČÚZK does
        not backfill older months, so they would bring nothing anyway."""
        if not self.processed_months:
            return
        if self.last_snapshot is None:
            stuck = set(self.processed_months)
        else:
            last_dir = self.last_snapshot_month
            stuck = {m for m in self.processed_months if m > last_dir}
        if stuck:
            self.processed_months = [m for m in self.processed_months if m not in stuck]
