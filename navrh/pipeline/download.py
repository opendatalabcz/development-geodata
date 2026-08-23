"""Stahování měsíčních VFR souborů (OB_UKSH) po jedné obci ze
services.cuzk.gov.cz/vfr – bez ukládání meziproduktů na disk.

Adresářová struktura serveru: /vfr/{RRRRMM}/{RRRRMMDD}_OB_{kod_obce}_UKSH.xml.*
Přípona se v čase měnila (.xml.gz do cca 2020, později .xml.zip), proto se
skutečný název souboru vždy zjišťuje z výpisu adresáře daného měsíce –
nikdy se neskládá "naslepo" podle šablony.
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

from .vfr_parser import parse_stavebni_objekty

BASE_URL = "https://services.cuzk.gov.cz/vfr"
USER_AGENT = (
    "bakalarka-ruian-pipeline/0.1 "
    "CVUT FIT"
)

_FILE_ROW_RE = re.compile(r'<a href="([^"]+)">\1</a>')


@dataclass
class ObecSnapshotResult:
    yyyymm: str
    kod_obec: str
    filename: str | None
    gdf: gpd.GeoDataFrame | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.gdf is not None


def list_month_files(yyyymm: str, session: requests.Session | None = None) -> list[str]:
    """Vrátí seznam všech názvů souborů v adresáři /vfr/{yyyymm}/."""
    session = session or requests
    resp = session.get(f"{BASE_URL}/{yyyymm}/", headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return _FILE_ROW_RE.findall(resp.text)


def _match_obec_filename(kod_obec: str, filenames: list[str]) -> str | None:
    """Vybere z výpisu adresáře (viz `list_month_files`) přesný název
    OB_<kod_obec>_UKSH souboru (nebo None) – čistá funkce, žádné I/O."""
    pattern = re.compile(rf"^\d{{8}}_OB_{re.escape(kod_obec)}_UKSH\.xml\.(zip|gz)$")
    for name in filenames:
        if pattern.match(name):
            return name
    return None


def find_obec_filename(yyyymm: str, kod_obec: str, session: requests.Session | None = None) -> str | None:
    """Najde přesný název OB_<kod_obec>_UKSH souboru v daném měsíci (nebo None)."""
    return _match_obec_filename(kod_obec, list_month_files(yyyymm, session=session))


class MonthListingCache:
    """Sdílená mezipaměť výpisů adresářů /vfr/{yyyymm}/ napříč obcemi.

    Výpis adresáře daného měsíce je pro všechny obce stejný, takže při
    zpracování víc obcí najednou (viz `build_region.py`) ho stačí stáhnout
    jednou a dál jen znovupoužívat – místo aby si ho každá obec tahala
    zvlášť. Thread-safe (víc vláken se může ptát na stejný/různý měsíc
    zároveň), viz `ThreadPoolExecutor` v `build_region.py`.
    """

    def __init__(self) -> None:
        self._data: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def get(self, yyyymm: str, session: requests.Session | None = None) -> list[str]:
        with self._lock:
            cached = self._data.get(yyyymm)
        if cached is not None:
            return cached
        # síťové volání záměrně mimo zámek, ať se navzájem neblokují vlákna
        # čekající na jiné měsíce (příp. duplicitní stažení téhož měsíce
        # dvěma vlákny najednou vadit nebude, jen se zahodí jeden výsledek)
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
    raise ValueError(f"neznámá přípona souboru: {filename}")


def fetch_obec_snapshot(
    yyyymm: str,
    kod_obec: str,
    session: requests.Session | None = None,
    listing_cache: MonthListingCache | None = None,
) -> ObecSnapshotResult:
    """Stáhne a naparsuje jeden měsíční snapshot obce – vše v paměti,
    na disk se nic neukládá.

    `listing_cache`: pokud je dodaná, použije se pro zjištění výpisu
    adresáře daného měsíce místo vlastního dotazu (viz `MonthListingCache`)
    – vyplatí se při zpracování víc obcí najednou."""
    session = session or requests
    try:
        try:
            if listing_cache is not None:
                filename = _match_obec_filename(kod_obec, listing_cache.get(yyyymm, session=session))
            else:
                filename = find_obec_filename(yyyymm, kod_obec, session=session)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                # adresář měsíce ještě neexistuje (budoucí/nezveřejněný měsíc)
                return ObecSnapshotResult(yyyymm, kod_obec, None, None, error="soubor pro tento měsíc neexistuje")
            raise
        if filename is None:
            return ObecSnapshotResult(yyyymm, kod_obec, None, None, error="soubor pro tento měsíc neexistuje")

        url = f"{BASE_URL}/{yyyymm}/{filename}"
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
        resp.raise_for_status()

        xml_bytes = _extract_xml_bytes(resp.content, filename)
        gdf = parse_stavebni_objekty(io.BytesIO(xml_bytes), source_name=filename)
        return ObecSnapshotResult(yyyymm, kod_obec, filename, gdf)
    except Exception as exc:  # síťová chyba, poškozený soubor apod. – nechceme spadnout celý běh
        return ObecSnapshotResult(yyyymm, kod_obec, None, None, error=str(exc))


def month_range(start: str = "201508", end: str | None = None) -> list[str]:
    """Vygeneruje seznam RRRRMM od `start` do `end` (včetně), výchozí `end` = aktuální měsíc."""
    y0, m0 = int(start[:4]), int(start[4:6])
    if end is None:
        today = date.today()
        y1, m1 = today.year, today.month
    else:
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


def fetch_obec_history_stream(
    kod_obec: str,
    months: list[str] | None = None,
    delay_s: float = 1.0,
    session: requests.Session | None = None,
    listing_cache: MonthListingCache | None = None,
):
    """Generátor: postupně stahuje a parsuje snapshoty jedné obce měsíc po
    měsíci (slušné tempo dotazů – `delay_s` mezi requesty), a jeden po druhém
    je vydává (yield). Nic se neukládá na disk – volající si stav ukládá sám
    (viz history.py)."""
    months = months or month_range()
    session = session or requests.Session()

    for i, yyyymm in enumerate(months):
        result = fetch_obec_snapshot(yyyymm, kod_obec, session=session, listing_cache=listing_cache)
        yield result
        if i < len(months) - 1:
            time.sleep(delay_s)
