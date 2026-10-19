"""Universal pipeline run: downloads and processes the chosen range of monthly
RÚIAN snapshots (OB_UKSH) for one municipality, a list of municipalities or a
whole named region, and builds the versioned history of building objects (SCD2)
out of them. The month range is given via `--start` / `--end`, municipalities
are processed concurrently, and a single combined GeoParquet export
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

DELAY_S_DEFAULT = 0.3
WORKERS_DEFAULT = 8

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "navrh" / "data" / "history"
OUT_DIR = REPO_ROOT / "navrh" / "output"

logger = logging.getLogger(__name__)


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
    month, and return its history plus a per-month summary. Runs in a worker
    thread - the concurrency is across municipalities, not months."""
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
    hist = state.to_geodataframe()
    logger.info(
        "done %s (%s): %s processed (ok=%d, missing=%d, errors=%d), %d history rows",
        name, municipality_code, _plural(len(todo), "month"),
        ok_count, missing_count, error_count, len(hist),
    )
    skipped_in_range = state.skipped_in(months)
    series_start = min(state.processed_months) if state.processed_months else None
    inside = [m for m in skipped_in_range if series_start is not None and m > series_start]
    before = [m for m in skipped_in_range if m not in inside]
    if inside:
        logger.warning(
            "%s (%s): a hole remains in the series - %s (the source file could not be "
            "processed; it can only be filled in via --rebuild)",
            name, municipality_code, _format_months(inside),
        )
    if before:
        logger.info(
            "%s (%s): %s missing before the start of the series (the municipality had "
            "no data in RÚIAN back then, or the file could not be processed) - not tried again",
            name, municipality_code, _format_months(before),
        )
    return {
        "municipality_code": municipality_code,
        "name": name,
        "hist": hist,
        "ok": ok_count,
        "missing": missing_count,
        "errors": error_count,
    }


def _resolve_municipalities(args) -> tuple[list[tuple[str, str, float]], str]:
    """Pick the list of municipalities to process out of the arguments, plus the
    name of the combined export. `--municipality` takes precedence over
    `--region`; an unknown code keeps the code itself as its name."""
    if not args.municipality:
        municipalities = REGIONS[args.region]
        if args.limit:
            municipalities = municipalities[: args.limit]
        return municipalities, args.region

    names = {code: name for region in REGIONS.values() for code, name, _dist in region}
    codes = [c.strip() for c in args.municipality.split(",") if c.strip()]
    municipalities = [(code, names.get(code, code), 0.0) for code in codes]
    name = codes[0] if len(codes) == 1 else f"municipalities_{len(codes)}"
    return municipalities, name


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


def _report_clamp(args, months: list[str], latest: str) -> None:
    """Report whether the given range had to be trimmed to the available
    archive."""
    requested_start = parse_month(args.start)
    if requested_start < months[0]:
        logger.info(
            "the start %s lies before the start of the ČÚZK archive (%s) - range trimmed to %s.",
            requested_start, ARCHIVE_START, months[0],
        )
    if args.end is not None:
        requested_end = parse_month(args.end)
        if requested_end > months[-1]:
            logger.info(
                "the end %s is not published yet (the last one is %s) - range trimmed to %s.",
                requested_end, latest, months[-1],
            )


def _check_range(municipalities: list[tuple[str, str, float]], months: list[str], args) -> bool:
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

    if skipped and not args.rebuild:
        logger.info("these months will be skipped - their source file already failed to "
                    "process once and is not tried again (will be tried again with --rebuild):")
        _log_municipalities(skipped, "", logging.INFO)

    if out_of_order and not args.rebuild:
        ok = False
        logger.error(
            "the range reaches before the already processed state. Such snapshots cannot "
            "be added after the fact - building the history (SCD2) depends on the order, "
            "so ingest() would have to drop them."
        )
        _log_municipalities(out_of_order, "", logging.ERROR)
        logger.error("Fix: set --start past the current state, or use --rebuild "
                     "(the history is built again from scratch).")

    if gaps and not args.rebuild:
        ok = False
        logger.error(
            "after this run a hole would remain in the series of months. An object that "
            "disappeared inside the hole gets its valid_to only from the first snapshot "
            "past it, so the history would be distorted. These months are on the server - "
            "the range just skipped over them."
        )
        _log_municipalities(gaps, "missing ", logging.ERROR)
        first_gap = min(m for hole in gaps.values() for m in hole)
        logger.error("Fix: --start %s (the earliest month missing anywhere above), or --rebuild.",
                     first_gap)

    return ok


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    """Count with a noun in the right number: 1 month / 5 months."""
    if n == 1:
        return f"{n} {singular}"
    return f"{n} {plural or singular + 's'}"


def _format_months(months: list[str], max_listed: int = 6) -> str:
    """The number of months and which ones they are ('1 month: 201605',
    '12 months: 202501-202512'). A contiguous series is shortened to `from-to`,
    a non-contiguous one is listed month by month."""
    count = _plural(len(months), "month")
    if len(months) == 1:
        return f"{count}: {months[0]}"
    if months == month_range(months[0], months[-1], clamp=False):
        return f"{count}: {months[0]}-{months[-1]}"
    listing = ", ".join(months[:max_listed])
    if len(months) > max_listed:
        listing += ", ..."
    return f"{count}: {listing}"


def _log_municipalities(
    problems: dict[str, list[str]], prefix: str, level: int, max_rows: int = 5
) -> None:
    """Log the first few problematic municipalities - a whole region would
    drown the error message itself."""
    for where, months in list(problems.items())[:max_rows]:
        logger.log(level, "  %s: %s%s", where, prefix, _format_months(months))
    if len(problems) > max_rows:
        logger.log(level, "  ... and %d more municipalities", len(problems) - max_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default="brno_area", choices=sorted(REGIONS),
                         help="which named region to process (see regions.py); ignored with --municipality")
    parser.add_argument("--municipality", "--obec", default=None,
                         help="process only the given municipality/municipalities by RÚIAN code, comma separated (e.g. 539309) - instead of a whole region")
    parser.add_argument("--limit", type=int, default=None,
                         help="process only the first N municipalities of the region (they are ordered by distance from the centre) - for a quick test")
    parser.add_argument("--workers", type=int, default=WORKERS_DEFAULT,
                         help=f"how many municipalities to download concurrently (default {WORKERS_DEFAULT})")
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    municipalities, default_name = _resolve_municipalities(args)
    out_name = args.name or default_name

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=max(args.workers, 10), pool_maxsize=max(args.workers, 10)
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    latest, latest_known = _resolve_latest_month(session)

    try:
        months = month_range(start=args.start, end=args.end, latest=latest)
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(2)

    if not months:
        if parse_month(args.start) > latest:
            logger.info("Nothing to do: %s is not published yet (the last available one is %s).",
                        parse_month(args.start), latest)
            sys.exit(0)
        logger.error("the given range lies entirely outside the available archive "
                     "(%s-%s), there is nothing to download.", ARCHIVE_START, latest)
        sys.exit(2)

    _report_clamp(args, months, latest)

    if not _check_range(municipalities, months, args):
        sys.exit(1)

    municipality_count = _plural(len(municipalities), "municipality", "municipalities")
    where = f"municipality {municipalities[0][1]}" if len(municipalities) == 1 else (
        municipality_count if args.municipality else f"region '{args.region}' ({municipality_count})")
    logger.info(
        "%s, %s, %d concurrent threads, delay=%ss/request%s",
        where, _format_months(months), args.workers, args.delay,
        ", REBUILD (the state is built from scratch)" if args.rebuild else "",
    )

    retry_from = latest if latest_known else months[0]

    listing_cache = MonthListingCache()

    t0 = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_process_municipality, code, name, months, session, listing_cache,
                        args.delay, retry_from, args.rebuild): (code, name)
            for code, name, _dist in municipalities
        }
        for future in as_completed(futures):
            code, name = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                logger.error("[%s %s] FAILED: %s", name, code, exc, exc_info=True)

    elapsed = time.monotonic() - t0
    logger.info(
        "Finished in %.1f min%s", elapsed / 60,
        f" ({elapsed / len(municipalities):.1f} s/municipality on average)."
        if len(municipalities) > 1 else ".",
    )

    if not results:
        logger.error("No municipality was processed successfully, quitting.")
        sys.exit(1)

    # -- combined export ------------------------------------------------
    frames = [r["hist"].assign(municipality_code=r["municipality_code"], municipality_name=r["name"])
              for r in results if len(r["hist"]) > 0]

    if not frames:
        logger.warning("No municipality has any data, the combined export is skipped.")
        return

    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    combined = combined.sort_values(["municipality_code", "code", "valid_from"]).reset_index(drop=True)

    name = f"{out_name}_history"
    write_geoparquet(combined, OUT_DIR / f"{name}.parquet")
    write_csv_and_geojson(combined, OUT_DIR, name,
                           keep_cols=["code", "municipality_code", "municipality_name",
                                      "valid_from", "valid_to", "end_reason"])

    logger.info(
        "Total: %d history rows across %s, %d unique objects.",
        len(combined), _plural(len(frames), "municipality", "municipalities"),
        combined["code"].nunique(),
    )
    logger.info("end_reason counts:\n%s",
                combined["end_reason"].value_counts(dropna=False).to_string())


if __name__ == "__main__":
    main()
