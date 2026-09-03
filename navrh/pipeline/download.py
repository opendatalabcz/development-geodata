"""Download of monthly VFR files (OB_UKSH), one municipality at a time, from
services.cuzk.gov.cz/vfr - without writing any intermediate files to disk. The
file name is always looked up in the directory listing of the given month,
never assembled from a template: the suffix changed over time (.xml.gz until
roughly 2020, .xml.zip later).
"""

from __future__ import annotations

import gzip
import io
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import date

import geopandas as gpd
import requests

from .vfr_parser import parse_building_objects

BASE_URL = "https://services.cuzk.gov.cz/vfr"
USER_AGENT = (
    "bakalarka-ruian-pipeline/0.1 "
    "CVUT FIT"
)

# the oldest month RÚIAN publishes
ARCHIVE_START = "201508"

MISSING_FILE_ERROR = "no source file for this month"

# <a href="20260601_OB_539309_UKSH.xml.zip">20260601_OB_539309_UKSH.xml.zip</a>
_FILE_ROW_RE = re.compile(r'<a href="([^"]+)">\1</a>')

# YYYYMM (6 digits) or YYYY-M / YYYY-MM
_MONTH_ARG_RE = re.compile(r"^(\d{4})(?:-(\d{1,2})|(\d{2}))$")

# <a href="/vfr/202606">202606</a>
_MONTH_DIR_RE = re.compile(r'<a href="[^"]*">(\d{6})</a>')


@dataclass
class MunicipalitySnapshotResult:
    yyyymm: str
    municipality_code: str
    filename: str | None
    gdf: gpd.GeoDataFrame | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.gdf is not None

    @property
    def missing(self) -> bool:
        """True when the source file simply is not on the server (as opposed to
        a download or parse failure)."""
        return self.error == MISSING_FILE_ERROR


def list_month_files(yyyymm: str, session: requests.Session | None = None) -> list[str]:
    """Return the names of all files in the directory /vfr/{yyyymm}/."""
    session = session or requests
    resp = session.get(f"{BASE_URL}/{yyyymm}/", headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return _FILE_ROW_RE.findall(resp.text)


def _match_municipality_filename(municipality_code: str, filenames: list[str]) -> str | None:
    """Pick the exact name of the OB_<municipality_code>_UKSH file out of a
    directory listing, or None."""
    pattern = re.compile(rf"^\d{{8}}_OB_{re.escape(municipality_code)}_UKSH\.xml\.(zip|gz)$")
    for name in filenames:
        if pattern.match(name):
            return name
    return None


def find_municipality_filename(
    yyyymm: str,
    municipality_code: str,
    session: requests.Session | None = None,
) -> str | None:
    """Find the exact name of the OB_<municipality_code>_UKSH file in the given
    month (or None)."""
    return _match_municipality_filename(municipality_code, list_month_files(yyyymm, session=session))


class MonthListingCache:
    """Shared cache of the /vfr/{yyyymm}/ directory listings across
    municipalities. The listing is the same for every municipality in a month,
    so it is downloaded once and reused."""

    def __init__(self) -> None:
        self._data: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def get(self, yyyymm: str, session: requests.Session | None = None) -> list[str]:
        with self._lock:
            cached = self._data.get(yyyymm)
        if cached is not None:
            return cached
        # the network call stays outside the lock, so two threads may fetch the
        # same month - one result is then thrown away
        files = list_month_files(yyyymm, session=session)
        with self._lock:
            self._data.setdefault(yyyymm, files)
            return self._data[yyyymm]


def _extract_xml_bytes(raw: bytes, filename: str) -> bytes:
    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            inner = next(n for n in zf.namelist() if n.endswith(".xml"))
            return zf.read(inner)
    if filename.endswith(".gz"):
        return gzip.decompress(raw)
    raise ValueError(f"unknown file suffix: {filename}")


def fetch_municipality_snapshot(
    yyyymm: str,
    municipality_code: str,
    session: requests.Session | None = None,
    listing_cache: MonthListingCache | None = None,
) -> MunicipalitySnapshotResult:
    """Download and parse a single monthly snapshot of one municipality - all
    in memory, nothing is written to disk."""
    session = session or requests
    try:
        try:
            if listing_cache is not None:
                filename = _match_municipality_filename(
                    municipality_code, listing_cache.get(yyyymm, session=session)
                )
            else:
                filename = find_municipality_filename(yyyymm, municipality_code, session=session)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                # the month directory does not exist yet (future/unpublished month)
                return MunicipalitySnapshotResult(
                    yyyymm, municipality_code, None, None, error=MISSING_FILE_ERROR
                )
            raise
        if filename is None:
            return MunicipalitySnapshotResult(
                yyyymm, municipality_code, None, None, error=MISSING_FILE_ERROR
            )

        url = f"{BASE_URL}/{yyyymm}/{filename}"
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
        resp.raise_for_status()

        xml_bytes = _extract_xml_bytes(resp.content, filename)
        gdf = parse_building_objects(io.BytesIO(xml_bytes), source_name=filename)
        return MunicipalitySnapshotResult(yyyymm, municipality_code, filename, gdf)
    except Exception as exc:
        return MunicipalitySnapshotResult(yyyymm, municipality_code, None, None, error=str(exc))


def list_available_months(session: requests.Session | None = None) -> list[str]:
    """Return the sorted list of months (YYYYMM) for which ČÚZK publishes a
    directory."""
    session = session or requests
    resp = session.get(f"{BASE_URL}/", headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return sorted(set(_MONTH_DIR_RE.findall(resp.text)))


def latest_available_month(session: requests.Session | None = None) -> str:
    """The newest month ČÚZK has actually published."""
    months = list_available_months(session=session)
    if not months:
        raise ValueError("the /vfr/ listing returned no month")
    return months[-1]


def parse_month(value: str) -> str:
    """Normalise a month into the canonical YYYYMM form."""
    text = str(value).strip()
    match = _MONTH_ARG_RE.match(text)
    if match is None:
        raise ValueError(f"invalid month: {value!r} (expected YYYYMM or YYYY-M)")
    year = int(match.group(1))
    month = int(match.group(2) or match.group(3))
    if not 1 <= month <= 12:
        raise ValueError(f"invalid month in {value!r}: {month} (expected 1-12)")
    return f"{year:04d}{month:02d}"


def current_month() -> str:
    """The current month as YYYYMM."""
    return date.today().strftime("%Y%m")


def month_range(
    start: str = ARCHIVE_START,
    end: str | None = None,
    clamp: bool = True,
    latest: str | None = None,
) -> list[str]:
    """Generate the list of YYYYMM from `start` to `end` (inclusive). With
    `clamp=True` (the default) the range is trimmed to what can exist on the
    server - `ARCHIVE_START` at the bottom, `latest` at the top - and may come
    out empty; a reversed range raises ValueError."""
    latest = parse_month(latest) if latest is not None else current_month()
    start = parse_month(start)
    end = parse_month(end) if end is not None else latest
    if start > end:
        raise ValueError(f"empty range: start {start} lies past the end {end}")

    if clamp:
        start = max(start, ARCHIVE_START)
        end = min(end, latest)
        if start > end:
            return []

    y0, m0 = int(start[:4]), int(start[4:6])
    y1, m1 = int(end[:4]), int(end[4:6])

    months = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        months.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def fetch_municipality_history_stream(
    municipality_code: str,
    months: list[str] | None = None,
    delay_s: float = 1.0,
    session: requests.Session | None = None,
    listing_cache: MonthListingCache | None = None,
):
    """Generator: downloads and parses the snapshots of one municipality month
    by month, `delay_s` apart, and yields them one after another. Nothing is
    written to disk - the caller keeps the state itself."""
    session = session or requests.Session()
    months = months or month_range(latest=latest_available_month(session=session))

    for i, yyyymm in enumerate(months):
        result = fetch_municipality_snapshot(
            yyyymm, municipality_code, session=session, listing_cache=listing_cache
        )
        yield result
        if i < len(months) - 1:
            time.sleep(delay_s)
