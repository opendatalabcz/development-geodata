"""Pipeline run that stores the monthly RÚIAN snapshots (OB_UKSH) as change
events partitioned by month (see month_events_spec.md) and derives the versioned
history (SCD2) of building objects from them. Municipalities and chunks of
months are processed in parallel worker processes; the month range is given via
`--start` / `--end` and a later run only adds the months that are still missing.

Usage (from the repository root):
    python -m navrh.pipeline.build_events --municipality 539309
    python -m navrh.pipeline.build_events --municipality 539309 --start 2026-7 --end 2026-7
    python -m navrh.pipeline.build_events --region brno_area --workers 12 --chunk-months 6
"""

from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

import geopandas as gpd
import pandas as pd
import requests

from .build_region import (
    DELAY_S_DEFAULT,
    OUT_DIR,
    REPO_ROOT,
    WORKERS_DEFAULT,
    _configure_logging,
    _export_name,
    _log_municipalities,
    _report_clamp,
    _resolve_latest_month,
    _resolve_municipalities,
    build_parser,
)
from .download import (
    ARCHIVE_START,
    MISSING_FILE_ERROR,
    MonthListingCache,
    _match_municipality_filename,
    fetch_snapshot_file,
    month_range,
    parse_month,
)
from .event_store import PENDING, PROCESSED, SKIPPED, EventStore, status_record
from .events import ADDED, CHANGED, REMOVED, derive_history, snapshot_events
from .export_parquet import write_geoparquet

CHUNK_MONTHS_DEFAULT = 12
# if the predecessor of a chunk is damaged, this many earlier months are tried
FALLBACK_PREDECESSORS = 3
LISTING_THREADS = 8
HISTORY_BATCH = 100

HISTORY_DIR = REPO_ROOT / "navrh" / "data" / "history_from_events"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Task:
    """The events of `months` (consecutive available months) of one
    municipality. `predecessors` are the months to diff the first month
    against, the newest first (empty for the very first month of a series);
    `filenames` maps every month involved to its source file."""
    municipality_code: str
    months: tuple[str, ...]
    predecessors: tuple[str, ...]
    filenames: dict[str, str]


# -- planning ---------------------------------------------------------------

def _failure_status(yyyymm: str, retry_from: str) -> str:
    """A month at or after `retry_from` (the newest published one) may still
    show up and stays pending; an earlier one is final."""
    return PENDING if yyyymm >= retry_from else SKIPPED


def _months_by_status(status: pd.DataFrame) -> dict[str, dict[str, list[str]]]:
    """municipality -> status -> sorted months."""
    result: dict[str, dict[str, list[str]]] = {}
    for (code, state), group in status.groupby(["municipality_code", "status"]):
        result.setdefault(code, {})[state] = sorted(group["yyyymm"])
    return result


def check_range(
    municipalities: list[tuple[str, str, float]], months: list[str], status: pd.DataFrame, rebuild: bool
) -> bool:
    """Verify that the range can be attached to what is already stored for
    every municipality: it must not reach before the stored months (their
    events were computed against their own predecessors) and must not leave a
    hole after them. Returns True when the run may go on."""
    known = _months_by_status(status)
    out_of_order: dict[str, list[str]] = {}
    gaps: dict[str, list[str]] = {}
    for code, name, _dist in municipalities:
        done = known.get(code, {}).get(PROCESSED, [])
        if not done or rebuild:
            continue
        skipped = set(known[code].get(SKIPPED, []))
        where = f"{name} ({code})"
        stale = [m for m in months if m < done[0] and m not in skipped]
        if stale:
            out_of_order[where] = stale
        hole = [m for m in month_range(done[-1], months[0], clamp=False)[1:-1] if m not in skipped] \
            if months[0] > done[-1] else []
        if hole:
            gaps[where] = hole

    if out_of_order:
        logger.error("the range reaches before the already stored months; events cannot be added "
                     "there after the fact (use --rebuild to start these municipalities again):")
        _log_municipalities(out_of_order, logging.ERROR)
    if gaps:
        logger.error("after this run a hole would remain in the series of months:")
        _log_municipalities(gaps, logging.ERROR)
        logger.error("Fix: --start %s (the earliest month missing anywhere above), or --rebuild.",
                     min(m for hole in gaps.values() for m in hole))
    return not (out_of_order or gaps)


def plan_tasks(
    municipalities: list[tuple[str, str, float]],
    months: list[str],
    status: pd.DataFrame,
    get_listing,
    chunk_months: int,
    retry_from: str,
) -> tuple[list[Task], list[dict]]:
    """Split the work into tasks and return them together with the status
    records of the months that have no source file (nothing to download there).
    `get_listing(yyyymm)` returns the file names of a month; `chunk_months` is
    the longest chunk (0 = the whole range of a municipality in one task)."""
    known = _months_by_status(status)
    tasks: list[Task] = []
    records: list[dict] = []
    for code, _name, _dist in municipalities:
        state = known.get(code, {})
        settled = set(state.get(PROCESSED, [])) | set(state.get(SKIPPED, []))
        filenames: dict[str, str] = {}
        todo: list[str] = []
        for yyyymm in months:
            if yyyymm in settled:
                continue
            filename = _match_municipality_filename(code, get_listing(yyyymm))
            if filename is None:
                records.append(status_record(code, yyyymm, _failure_status(yyyymm, retry_from),
                                             error=MISSING_FILE_ERROR))
            else:
                filenames[yyyymm] = filename
                todo.append(yyyymm)
        if not todo:
            continue

        step = chunk_months or len(todo)
        earlier = sorted(state.get(PROCESSED, []), reverse=True)  # newest first
        for i in range(0, len(todo), step):
            chunk = todo[i:i + step]
            predecessors = []
            for yyyymm in earlier[:1 + FALLBACK_PREDECESSORS]:
                filename = _match_municipality_filename(code, get_listing(yyyymm))
                if filename is not None:
                    filenames[yyyymm] = filename
                    predecessors.append(yyyymm)
            tasks.append(Task(code, tuple(chunk), tuple(predecessors),
                              {m: filenames[m] for m in (*chunk, *predecessors)}))
            earlier = [*reversed(chunk), *earlier]
    return tasks, records


# -- running a task ---------------------------------------------------------

_worker_session: requests.Session | None = None
_worker_store: EventStore | None = None


def _init_worker(verbose: bool, store_root) -> None:
    """Initializer of every worker process (a spawned process starts without
    the logging setup, and sessions cannot be shared between processes)."""
    global _worker_session, _worker_store
    _configure_logging(verbose)
    _worker_session = requests.Session()
    _worker_store = EventStore(store_root)


def _run_task(task: Task, run_id: str, delay_s: float, retry_from: str) -> dict:
    return process_task(task, run_id, _worker_store, _worker_session, delay_s, retry_from)


def _snapshot_or_error(
    yyyymm: str, task: Task, session: requests.Session
) -> tuple[gpd.GeoDataFrame | None, str | None]:
    result = fetch_snapshot_file(yyyymm, task.municipality_code, task.filenames[yyyymm], session=session)
    if not result.ok:
        return None, result.error
    if len(result.gdf) == 0:
        return None, "empty snapshot"
    return result.gdf, None


def process_task(
    task: Task, run_id: str, store: EventStore, session: requests.Session, delay_s: float, retry_from: str
) -> dict:
    """Download the months of one task one after another, keep only the
    previous and the current snapshot in memory, and write the events and the
    status of every month into the staging directory of the store."""
    code = task.municipality_code
    previous = None
    for yyyymm in task.predecessors:
        previous, _error = _snapshot_or_error(yyyymm, task, session)
        if previous is not None:
            break
        time.sleep(delay_s)
    if task.predecessors and previous is None:
        raise RuntimeError(f"no usable predecessor of {task.months[0]} among {', '.join(task.predecessors)}")

    parts, records = [], []
    for i, yyyymm in enumerate(task.months):
        current, error = _snapshot_or_error(yyyymm, task, session)
        if current is None:
            logger.error("[%s %s] %s", code, yyyymm, error)
            records.append(status_record(code, yyyymm, _failure_status(yyyymm, retry_from), error=error))
        else:
            events = snapshot_events(current, previous, code, yyyymm)
            previous = current
            parts.append(events)
            counts = events["event_type"].value_counts()
            records.append(status_record(code, yyyymm, PROCESSED,
                                         tuple(int(counts.get(t, 0)) for t in (ADDED, CHANGED, REMOVED))))
        if i < len(task.months) - 1:
            time.sleep(delay_s)

    events = pd.concat(parts, ignore_index=True) if parts else None
    store.write_task_output(run_id, f"{code}_{task.months[0]}-{task.months[-1]}", events, records)
    return {
        "municipality_code": code,
        "months": len(task.months),
        "ok": sum(r["status"] == PROCESSED for r in records),
        "failed": sum(r["status"] != PROCESSED for r in records),
        "events": 0 if events is None else len(events),
    }


# -- run --------------------------------------------------------------------

def _load_listings(months: list[str], cache: MonthListingCache, session: requests.Session) -> None:
    """Fetch the monthly directory listings up front, in parallel."""
    with ThreadPoolExecutor(max_workers=LISTING_THREADS) as pool:
        list(pool.map(lambda m: cache.get(m, session=session), months))


def export_history(
    store: EventStore, municipalities: list[tuple[str, str, float]], export_name: str, combined_export: bool
) -> None:
    """Derive the history of every municipality from its events (a batch of
    municipalities at a time, so memory stays bounded), write one file per
    municipality and optionally the combined export."""
    names = {code: name for code, name, _dist in municipalities}
    codes = list(names)
    frames = []
    for i in range(0, len(codes), HISTORY_BATCH):
        events = store.read_events(codes[i:i + HISTORY_BATCH])
        if events.empty:
            continue
        for code, municipality_events in events.groupby("municipality_code"):
            history = derive_history(municipality_events)
            write_geoparquet(history, HISTORY_DIR / f"{code}.parquet", quiet=True)
            if combined_export:
                frames.append(history.assign(municipality_code=code, municipality_name=names[code]))

    if not frames:
        if combined_export:
            logger.warning("No municipality has any data, the combined export is skipped.")
        return
    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    combined = combined.sort_values(["municipality_code", "code", "valid_from"]).reset_index(drop=True)
    name = f"{export_name}_history_from_events"
    write_geoparquet(combined, OUT_DIR / f"{name}.parquet")
    logger.info("Total: history rows=%d municipalities=%d unique objects=%d",
                len(combined), len(frames), combined["code"].nunique())


def main(
    municipalities: list[tuple[str, str, float]],
    export_name: str,
    start: str = ARCHIVE_START,
    end: str | None = None,
    workers: int = WORKERS_DEFAULT,
    delay_s: float = DELAY_S_DEFAULT,
    rebuild: bool = False,
    chunk_months: int = CHUNK_MONTHS_DEFAULT,
    combined_export: bool = True,
    verbose: bool = False,
    store: EventStore | None = None,
) -> None:
    """Add the events of the months `start`-`end` (default: the whole archive
    up to the last published month) for the given municipalities to the store
    and write the derived history."""
    store = store or EventStore()
    session = requests.Session()
    latest, latest_known = _resolve_latest_month(session)
    months = month_range(start=start, end=end, latest=latest)
    if not months:
        if parse_month(start) > latest:
            logger.info("Nothing to do: %s is not published yet (the last available one is %s).",
                        parse_month(start), latest)
            return
        raise ValueError(
            f"the given range lies entirely outside the available archive "
            f"({ARCHIVE_START}-{latest}), there is nothing to download."
        )
    _report_clamp(start, end, months, latest)

    store.compact()  # finish whatever an interrupted run left behind
    if rebuild:
        store.compact(forget=[code for code, _name, _dist in municipalities])
    if not check_range(municipalities, months, store.load_status(), rebuild):
        raise RuntimeError("the range cannot be attached to the stored events (see above)")

    listing_cache = MonthListingCache()
    _load_listings(months, listing_cache, session)
    retry_from = latest if latest_known else months[0]
    tasks, missing = plan_tasks(
        municipalities, months, store.load_status(),
        lambda m: listing_cache.get(m, session=session), chunk_months, retry_from,
    )
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    if missing:
        store.write_task_output(run_id, "planner", None, missing)

    logger.info(
        "municipalities=%d months=%s..%s (%d) tasks=%d chunk_months=%d workers=%d delay=%ss rebuild=%s",
        len(municipalities), months[0], months[-1], len(months), len(tasks), chunk_months,
        workers, delay_s, rebuild,
    )

    t0 = time.monotonic()
    ok = failed = n_events = errors = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(verbose, store.root)) as pool:
        futures = {pool.submit(_run_task, task, run_id, delay_s, retry_from): task for task in tasks}
        for done, future in enumerate(as_completed(futures), 1):
            task = futures[future]
            try:
                summary = future.result()
            except Exception as exc:
                errors += 1
                logger.error("[%s %s..%s] FAILED: %s", task.municipality_code, task.months[0],
                             task.months[-1], exc, exc_info=True)
                continue
            ok += summary["ok"]
            failed += summary["failed"]
            n_events += summary["events"]
            if done % max(1, len(tasks) // 20) == 0 or done == len(tasks):
                logger.info("tasks %d/%d (%.1f min)", done, len(tasks), (time.monotonic() - t0) / 60)

    logger.info("Processed in %.1f min: months ok=%d failed=%d (+%d without source file), events=%d, failed tasks=%d",
                (time.monotonic() - t0) / 60, ok, failed, len(missing), n_events, errors)

    store.compact()
    export_history(store, municipalities, export_name, combined_export)


def build_events_parser():
    parser = build_parser()
    parser.description = __doc__
    parser.add_argument("--chunk-months", type=int, default=CHUNK_MONTHS_DEFAULT,
                        help=f"longest run of months handled by one task (default {CHUNK_MONTHS_DEFAULT}; "
                             "0 = the whole range of a municipality in one task)")
    parser.add_argument("--no-combined-export", action="store_true",
                        help="write only the per-municipality histories, not one combined file "
                             "(the combined export holds everything in memory)")
    return parser


if __name__ == "__main__":
    cli_args = build_events_parser().parse_args()
    _configure_logging(cli_args.verbose)
    try:
        selected = _resolve_municipalities(cli_args.municipality, cli_args.region, cli_args.limit)
        main(
            selected,
            export_name=_export_name(cli_args.name, cli_args.municipality, cli_args.region, selected),
            start=cli_args.start,
            end=cli_args.end,
            workers=cli_args.workers,
            delay_s=cli_args.delay,
            rebuild=cli_args.rebuild,
            chunk_months=cli_args.chunk_months,
            combined_export=not cli_args.no_combined_export,
            verbose=cli_args.verbose,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(2)
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)
