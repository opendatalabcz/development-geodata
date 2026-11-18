"""On-disk storage of the change events (see month_events_spec.md):

    <root>/yyyymm=YYYYMM/events.parquet   one file per month, all municipalities
    <root>/status.parquet                 state of every (municipality, month)
    <root>/_staging/<run_id>/             output of tasks, before compaction

A task writes its events and its status records into the staging directory;
`compact` folds finished tasks into the month files and the status table and
removes the staging directory. A task counts as finished only once its status
file exists, so a crashed run leaves nothing half-done behind.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from collections.abc import Callable, Collection
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .export_parquet import write_geoparquet
from .vfr_parser import coerce_dtypes

EVENTS_DIR = Path(__file__).resolve().parent.parent / "data" / "events"

PROCESSED, PENDING, SKIPPED = "processed", "pending", "skipped"
STATUS_COLUMNS = ["municipality_code", "yyyymm", "status", "n_added", "n_changed", "n_removed", "error"]


def status_record(
    municipality_code: str,
    yyyymm: str,
    status: str,
    counts: tuple[int, int, int] = (0, 0, 0),
    error: str | None = None,
) -> dict:
    return dict(zip(STATUS_COLUMNS, (municipality_code, yyyymm, status, *counts, error)))


def _loosen_geo_metadata(schema: pa.Schema) -> pa.Schema:
    """The GeoParquet metadata of a staging file describes only its own rows;
    drop the bounding boxes and geometry types so they do not lie about a
    merged file."""
    metadata = dict(schema.metadata or {})
    if b"geo" in metadata:
        geo = json.loads(metadata[b"geo"])
        for column in geo.get("columns", {}).values():
            column.pop("bbox", None)
            column["geometry_types"] = []
        metadata[b"geo"] = json.dumps(geo).encode()
    return schema.with_metadata(metadata)


class EventStore:
    def __init__(self, root: str | Path = EVENTS_DIR) -> None:
        self.root = Path(root)

    # -- paths -------------------------------------------------------------

    def month_path(self, yyyymm: str) -> Path:
        return self.root / f"yyyymm={yyyymm}" / "events.parquet"

    def month_paths(self) -> list[Path]:
        return sorted(self.root.glob("yyyymm=*/events.parquet"))

    @property
    def status_path(self) -> Path:
        return self.root / "status.parquet"

    @property
    def _staging_root(self) -> Path:
        return self.root / "_staging"

    # -- status ------------------------------------------------------------

    def load_status(self) -> pd.DataFrame:
        if not self.status_path.exists():
            return pd.DataFrame(columns=STATUS_COLUMNS)
        return pd.read_parquet(self.status_path)

    def _save_status(self, status: pd.DataFrame) -> None:
        tmp = self.status_path.with_suffix(".parquet.tmp")
        status.to_parquet(tmp, index=False)
        tmp.replace(self.status_path)

    def _update_status(self, records: list[dict], forget: Collection[str]) -> None:
        status = self.load_status()
        status = status[~status["municipality_code"].isin(forget)]
        merged = pd.concat([status, pd.DataFrame(records, columns=STATUS_COLUMNS)], ignore_index=True)
        merged = merged.drop_duplicates(["municipality_code", "yyyymm"], keep="last")
        self._save_status(merged.sort_values(["municipality_code", "yyyymm"]).reset_index(drop=True))

    # -- output of tasks -----------------------------------------------------

    def write_task_output(
        self, run_id: str, name: str, events: gpd.GeoDataFrame | None, records: list[dict]
    ) -> None:
        """Write the events and the status records of one finished task. The
        status file goes last: it is what makes the task count as finished."""
        directory = self._staging_root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        events_file = None
        if events is not None and len(events) > 0:
            events_file = f"{name}.parquet"
            write_geoparquet(events, directory / events_file, quiet=True)
        tmp = directory / f"{name}.json.tmp"
        tmp.write_text(json.dumps({"events_file": events_file, "records": records}), encoding="utf-8")
        tmp.replace(directory / f"{name}.json")

    def _finished_tasks(self) -> list[tuple[Path | None, list[dict]]]:
        tasks = []
        for status_file in sorted(self._staging_root.glob("*/*.json")):
            content = json.loads(status_file.read_text(encoding="utf-8"))
            events_file = content["events_file"]
            tasks.append((status_file.parent / events_file if events_file else None, content["records"]))
        return tasks

    # -- compaction ----------------------------------------------------------

    def compact(self, forget: Collection[str] = ()) -> None:
        """Fold all finished tasks in the staging directory into the month
        files and the status table, then remove the staging directory. The
        rows of the municipalities in `forget` are removed from every month
        and from the status table first (for a rebuild)."""
        forget = set(forget)
        tasks = self._finished_tasks()
        if not tasks and not forget:
            return

        records = [record for _events_file, recs in tasks for record in recs]
        staged: dict[str, dict[str, Path]] = defaultdict(dict)  # month -> municipality -> events file
        replaced: dict[str, set[str]] = defaultdict(set)         # month -> municipalities with new results
        for events_file, recs in tasks:
            for record in recs:
                replaced[record["yyyymm"]].add(record["municipality_code"])
                if events_file is not None and record["n_added"] + record["n_changed"] + record["n_removed"] > 0:
                    staged[record["yyyymm"]][record["municipality_code"]] = events_file

        months = set(staged)
        if forget:
            months |= {path.parent.name.removeprefix("yyyymm=") for path in self.month_paths()}
        for yyyymm in sorted(months):
            self._write_month(yyyymm, staged.get(yyyymm, {}), replaced.get(yyyymm, set()) | forget)

        self._update_status(records, forget)
        shutil.rmtree(self._staging_root, ignore_errors=True)

    def _write_month(self, yyyymm: str, staged: dict[str, Path], drop: set[str]) -> None:
        """Rewrite one month file: keep the existing row groups of the
        municipalities not in `drop`, add the staged events, one row group per
        municipality in order of `municipality_code`. Only one municipality is
        held in memory at a time."""
        path = self.month_path(yyyymm)
        parts: dict[str, Callable[[], pa.Table]] = {}
        schemas: list[pa.Schema] = []
        existing = pq.ParquetFile(path) if path.exists() else None
        tmp = path.with_suffix(".parquet.tmp")

        try:
            if existing is not None:
                schemas.append(existing.schema_arrow)
                for i in range(existing.num_row_groups):
                    code = existing.read_row_group(i, columns=["municipality_code"])["municipality_code"][0].as_py()
                    if code not in drop:
                        parts[code] = lambda i=i: existing.read_row_group(i)
            for code, events_file in staged.items():
                schemas.append(pq.read_schema(events_file))
                parts[code] = lambda f=events_file: pq.read_table(f, filters=[("yyyymm", "=", yyyymm)])

            if parts:
                # a column that was entirely empty in one file can have a different
                # arrow type there, so the types are unified first
                schema = _loosen_geo_metadata(pa.unify_schemas(schemas, promote_options="permissive"))
                path.parent.mkdir(parents=True, exist_ok=True)
                with pq.ParquetWriter(tmp, schema) as writer:
                    for code in sorted(parts):
                        writer.write_table(parts[code]().cast(schema))
        finally:
            if existing is not None:
                existing.close()  # Windows cannot replace a file that is still open

        if parts:
            tmp.replace(path)
        else:
            path.unlink(missing_ok=True)

    # -- reading -------------------------------------------------------------

    def read_events(self, municipality_codes: Collection[str]) -> gpd.GeoDataFrame:
        """All events of the given municipalities over all months, with the
        final column types."""
        frames = [
            gpd.read_parquet(path, filters=[("municipality_code", "in", list(municipality_codes))])
            for path in self.month_paths()
        ]
        frames = [frame for frame in frames if len(frame) > 0]
        if not frames:
            return gpd.GeoDataFrame()
        return coerce_dtypes(pd.concat(frames, ignore_index=True))
