"""Universal pipeline run: downloads and processes the chosen range of monthly
RÚIAN snapshots (OB_UKSH) for one municipality, a list of municipalities or a
whole named region, and builds the versioned history of building objects (SCD2)
out of them. The month range is given via `--start` / `--end`, municipalities
are processed in parallel worker processes, and a single combined GeoParquet export
is produced at the end.

Usage (from the repository root):
    python -m navrh.pipeline.build_region --municipality 539309
    python -m navrh.pipeline.build_region --municipality 539309 --start 2026-7 --end 2026-7
    python -m navrh.pipeline.build_region --region brno_area --workers 12 --delay 0.3
    python -m navrh.pipeline.build_region --region brno_area --limit 10
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from .download import (
    ARCHIVE_START,
    MonthListingCache,
    current_month,
    fetch_municipality_snapshot,
    latest_available_month,
    month_range,
    parse_month,
)
from .export_parquet import write_geoparquet
from .export_tabular import write_csv_and_geojson
from .history import HistoryState
from .regions import REGIONS
from .vfr_parser import coerce_dtypes

DELAY_S_DEFAULT = 0.3
WORKERS_DEFAULT = 8

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "navrh" / "data" / "history"
OUT_DIR = REPO_ROOT / "navrh" / "output"

logger = logging.getLogger(__name__)

# set up once per worker process by _init_worker: a requests.Session and the
# listing cache cannot be shared between processes
_worker_session: requests.Session | None = None
_worker_listing_cache: MonthListingCache | None = None


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _init_worker(verbose: bool) -> None:
    """Initializer of every worker process. A fresh (spawned) process does not
    inherit the logging setup, so it is configured here too."""
    global _worker_session, _worker_listing_cache
    _configure_logging(verbose)
    _worker_session = requests.Session()
    _worker_listing_cache = MonthListingCache()


def _process_in_worker(
    municipality_code: str,
    name: str,
    months: list[str],
    delay_s: float,
    retry_from: str,
    rebuild: bool,
) -> dict:
    return _process_municipality(
        municipality_code, name, months, _worker_session, _worker_listing_cache,
        delay_s, retry_from, rebuild,
    )


def _process_municipality(
    municipality_code: str,
    name: str,
    months: list[str],
    session: requests.Session,
    listing_cache: MonthListingCache,
    delay_s: float,
    retry_from: str,
    rebuild: bool = False,
) -> dict:
    """Process the given month range of one municipality, sequentially month by
    month, and return a per-month summary; the history itself is left in the
    municipality's state file. Runs in a worker process - the concurrency is
    across municipalities, not months."""
    state_path = DATA_DIR / f"{municipality_code}.parquet"
    state = HistoryState() if rebuild else HistoryState.load(state_path)
    already_done = set(state.processed_months)
    # skipped months (missing or damaged) are not tried again
    # months not published yet stay in todo
    skipped = set(state.skipped_months)
    todo = [m for m in months if m not in already_done and m not in skipped]

    ok_count, missing_count, error_count = 0, 0, 0
    for i, yyyymm in enumerate(todo, 1):
        result = fetch_municipality_snapshot(
            yyyymm, municipality_code, session=session, listing_cache=listing_cache
        )
        if result.ok:
            state.ingest(result.gdf, yyyymm)
            ok_count += 1
        else:
            state.mark_failed(yyyymm, retry_from)
            if result.missing:
                missing_count += 1
            else:
                logger.error("[%s %s] %s", name, yyyymm, result.error)
                error_count += 1

        if i % 24 == 0 or i == len(todo):
            state.save(state_path)

        if i < len(todo):
            time.sleep(delay_s)

    state.save(state_path)
    logger.info(
        "done %s (%s): months=%d ok=%d missing=%d errors=%d, history rows=%d",
        name, municipality_code, len(todo), ok_count, missing_count, error_count, len(state.rows),
    )
    skipped_in_range = state.skipped_in(months)
    series_start = min(state.processed_months) if state.processed_months else None
    inside = [m for m in skipped_in_range if series_start is not None and m > series_start]
    before = [m for m in skipped_in_range if m not in inside]
    if inside:
        logger.warning(
            "%s (%s): hole in the series, %d months: %s (source file could not be "
            "processed; only --rebuild can fill it)",
            name, municipality_code, len(inside), ", ".join(inside),
        )
    if before:
        logger.info(
            "%s (%s): %d months missing before the start of the series "
            "(no data in RÚIAN back then, or the file could not be processed) - not retried: %s",
            name, municipality_code, len(before), ", ".join(before),
        )
    return {
        "municipality_code": municipality_code,
        "name": name,
        "ok": ok_count,
        "missing": missing_count,
        "errors": error_count,
    }


def _resolve_municipalities(
    municipality: str | None, region: str, limit: int | None
) -> list[tuple[str, str, float]]:
    """Pick the municipalities to process: the comma-separated RÚIAN codes in
    `municipality` if given, otherwise the named `region` (cut to the first
    `limit` municipalities). Each municipality is `(code, name,
    distance_from_centre)`; codes given by hand get their name from the known
    regions (or the code itself if unknown) and distance 0.0."""
    if not municipality:
        municipalities = REGIONS[region]
        if limit:
            municipalities = municipalities[:limit]
        return municipalities

    names = {code: name for reg in REGIONS.values() for code, name, _dist in reg}
    codes = [c.strip() for c in municipality.split(",") if c.strip()]
    return [(code, names.get(code, code), 0.0) for code in codes]


def _export_name(
    name: str | None,
    municipality: str | None,
    region: str,
    municipalities: list[tuple[str, str, float]],
) -> str:
    """Name of the combined export: `name` if given, otherwise the region
    name, the single municipality code, or `municipalities_<n>`."""
    if name:
        return name
    if not municipality:
        return region
    if len(municipalities) == 1:
        return municipalities[0][0]
    return f"municipalities_{len(municipalities)}"


def _resolve_latest_month(session: requests.Session) -> tuple[str, bool]:
    """The newest published month and whether it could actually be determined.
    When the /vfr/ listing is unavailable it falls back to the current calendar
    month and returns False, and the caller must then not declare any failed
    month final."""
    try:
        return latest_available_month(session=session), True
    except Exception as exc:
        fallback = current_month()
        logger.warning(
            "the /vfr/ listing could not be loaded (%s) - the current month %s is taken "
            "as the upper bound and all failed months will be tried again on the next run.",
            exc, fallback,
        )
        return fallback, False


def _report_clamp(start: str, end: str | None, months: list[str], latest: str) -> None:
    """Report whether the given range had to be trimmed to the available
    archive."""
    requested_start = parse_month(start)
    if requested_start < months[0]:
        logger.info(
            "the start %s lies before the start of the ČÚZK archive (%s) - range trimmed to %s.",
            requested_start, ARCHIVE_START, months[0],
        )
    if end is not None:
        requested_end = parse_month(end)
        if requested_end > months[-1]:
            logger.info(
                "the end %s is not published yet (the last one is %s) - range trimmed to %s.",
                requested_end, latest, months[-1],
            )


def _check_range(municipalities: list[tuple[str, str, float]], months: list[str], rebuild: bool) -> bool:
    """Verify that the given range can be attached to the current state of every
    municipality. Returns True when the run should go on. Checked up front from
    the stored progress, before anything starts downloading."""
    out_of_order: dict[str, list[str]] = {}
    gaps: dict[str, list[str]] = {}
    skipped: dict[str, list[str]] = {}
    for code, name, _dist in municipalities:
        progress = HistoryState.load_progress(DATA_DIR / f"{code}.parquet")
        where = f"{name} ({code})"
        stale = progress.out_of_order_months(months)
        if stale:
            out_of_order[where] = stale
        hole = progress.gap_months(months)
        if hole:
            gaps[where] = hole
        will_skip = progress.skipped_in(months)
        if will_skip:
            skipped[where] = will_skip

    ok = True

    if skipped and not rebuild:
        logger.info("these months will be skipped - their source file already failed to "
                    "process once and is not tried again (will be tried again with --rebuild):")
        _log_municipalities(skipped, logging.INFO)

    if out_of_order and not rebuild:
        ok = False
        logger.error(
            "the range reaches before the already processed state. Such snapshots cannot "
            "be added after the fact - building the history (SCD2) depends on the order, "
            "so ingest() would have to drop them."
        )
        _log_municipalities(out_of_order, logging.ERROR)
        logger.error("Fix: set --start past the current state, or use --rebuild "
                     "(the history is built again from scratch).")

    if gaps and not rebuild:
        ok = False
        logger.error(
            "after this run a hole would remain in the series of months. An object that "
            "disappeared inside the hole gets its valid_to only from the first snapshot "
            "past it, so the history would be distorted. These months are on the server - "
            "the range just skipped over them."
        )
        _log_municipalities(gaps, logging.ERROR)
        first_gap = min(m for hole in gaps.values() for m in hole)
        logger.error("Fix: --start %s (the earliest month missing anywhere above), or --rebuild.",
                     first_gap)

    return ok


def _log_municipalities(problems: dict[str, list[str]], level: int, max_rows: int = 5) -> None:
    """Log the first few problematic municipalities - a whole region would
    drown the error message itself."""
    for where, months in list(problems.items())[:max_rows]:
        logger.log(level, "  %s: %d months: %s", where, len(months), ", ".join(months))
    if len(problems) > max_rows:
        logger.log(level, "  ... and %d more municipalities", len(problems) - max_rows)


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser of the pipeline run."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default="brno_area", choices=sorted(REGIONS),
                         help="which named region to process (see regions.py); ignored with --municipality")
    parser.add_argument("--municipality", "--obec", default=None,
                         help="process only the given municipality/municipalities by RÚIAN code, comma separated (e.g. 539309) - instead of a whole region")
    parser.add_argument("--limit", type=int, default=None,
                         help="process only the first N municipalities of the region (they are ordered by distance from the centre) - for a quick test")
    parser.add_argument("--workers", type=int, default=WORKERS_DEFAULT,
                         help=f"how many municipalities to process in parallel, one worker process each (default {WORKERS_DEFAULT})")
    parser.add_argument("--delay", type=float, default=DELAY_S_DEFAULT,
                         help=f"pause between requests within one municipality, in seconds (default {DELAY_S_DEFAULT})")
    parser.add_argument("--start", default=ARCHIVE_START,
                         help=f"first month, YYYYMM or YYYY-M (default {ARCHIVE_START} = start of the ČÚZK archive)")
    parser.add_argument("--end", default=None,
                         help="last month, YYYYMM or YYYY-M (default: the last published month)")
    parser.add_argument("--name", default=None,
                         help="name of the combined export (default: after the region, resp. the municipality code)")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="log at DEBUG level")
    parser.add_argument("--rebuild", action="store_true",
                         help="throw away the current state and build the history from scratch (needed when the range reaches before the already processed state)")
    return parser


def main(
    municipalities: list[tuple[str, str, float]],
    export_name: str,
    start: str = ARCHIVE_START,
    end: str | None = None,
    workers: int = WORKERS_DEFAULT,
    delay_s: float = DELAY_S_DEFAULT,
    rebuild: bool = False,
    verbose: bool = False,
) -> None:
    """Download and process the months `start`-`end` (default: the whole
    archive up to the last published month) for the given municipalities and
    write the combined export `<export_name>_history`."""
    latest, latest_known = _resolve_latest_month(requests.Session())

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

    if not _check_range(municipalities, months, rebuild):
        raise RuntimeError("the range cannot be attached to the stored state (see above)")

    logger.info(
        "export=%s municipalities=%d months=%s..%s (%d) workers=%d delay=%ss rebuild=%s",
        export_name, len(municipalities), months[0], months[-1], len(months),
        workers, delay_s, rebuild,
    )

    retry_from = latest if latest_known else months[0]

    t0 = time.monotonic()
    results = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(verbose,)) as pool:
        futures = {
            pool.submit(_process_in_worker, code, name, months, delay_s, retry_from, rebuild): (code, name)
            for code, name, _dist in municipalities
        }
        for future in as_completed(futures):
            code, name = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                logger.error("[%s %s] FAILED: %s", name, code, exc, exc_info=True)

    elapsed = time.monotonic() - t0
    logger.info("Finished in %.1f min (%.1f s/municipality on average)",
                elapsed / 60, elapsed / len(municipalities))

    if not results:
        raise RuntimeError("no municipality was processed successfully")

    # -- combined export ------------------------------------------------
    frames = []
    for r in results:
        hist = coerce_dtypes(gpd.read_parquet(DATA_DIR / f"{r['municipality_code']}.parquet"))
        if len(hist) > 0:
            frames.append(hist.assign(municipality_code=r["municipality_code"], municipality_name=r["name"]))

    if not frames:
        logger.warning("No municipality has any data, the combined export is skipped.")
        return

    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    combined = combined.sort_values(["municipality_code", "code", "valid_from"]).reset_index(drop=True)

    name = f"{export_name}_history"
    write_geoparquet(combined, OUT_DIR / f"{name}.parquet")
    write_csv_and_geojson(combined, OUT_DIR, name,
                           keep_cols=["code", "municipality_code", "municipality_name",
                                      "valid_from", "valid_to", "end_reason"])

    logger.info(
        "Total: history rows=%d municipalities=%d unique objects=%d",
        len(combined), len(frames), combined["code"].nunique(),
    )
    logger.info("end_reason counts:\n%s",
                combined["end_reason"].value_counts(dropna=False).to_string())


if __name__ == "__main__":
    cli_args = build_parser().parse_args()
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
            verbose=cli_args.verbose,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(2)
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)
