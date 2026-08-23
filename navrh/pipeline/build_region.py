"""Rychlejší běh pipeliny na víc obcí najednou (region), ne jen na jednu.

Oproti `build_pilot_obec.py` přidává tři věci, kvůli kterým bylo zpracování
víc obcí po jedné zbytečně pomalé:

    1. sdílená `requests.Session` napříč celým během (keep-alive spojení
       místo nového TCP/TLS handshake na každý request)
    2. sdílená `MonthListingCache` (viz download.py) – výpis adresáře
       /vfr/{yyyymm}/ je pro daný měsíc stejný pro všechny obce, takže se
       stáhne jen jednou a dál jen znovupoužívá, místo aby si ho každá obec
       tahala zvlášť (dřív polovina všech requestů byla přesně tohle)
    3. víc obcí zpracovaných souběžně (`ThreadPoolExecutor`) – stahování je
       čekání na síť, ne na CPU, takže paralelizace škáluje skoro lineárně

Zpracování jedné obce zůstává sekvenční měsíc po měsíci (SCD2 diffování v
history.py na pořadí závisí) – paralelizuje se až napříč obcemi, které jsou
na sobě nezávislé. Jedna sdílená `Session` mezi vlákny je bezpečná, protože
se jen čte (žádné cookies/stav se neupravuje) a spojení pod ní (urllib3
`PoolManager`) je na víc vláken navržené.

Stav (`HistoryState`) se pro každou obec ukládá zvlášť do stejných
GeoParquet souborů jako `build_pilot_obec.py` (`navrh/data/history/<kod>.
parquet`), takže běh jde kdykoliv přerušit a znovu spustit – co je hotové,
se nestahuje znovu. Navíc na konci vznikne jeden souhrnný export za celý
region (GPKG + CSV + GeoJSON) se sloupci `obec_kod`/`obec_nazev`.

Použití (z kořene repozitáře):
    python -m navrh.pipeline.build_region
    python -m navrh.pipeline.build_region --workers 12 --delay 0.3
    python -m navrh.pipeline.build_region --region brno_okoli --limit 10
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from .download import MonthListingCache, fetch_obec_snapshot, month_range
from .export_gpkg import write_geopackage
from .export_tabular import write_csv_and_geojson
from .history import HistoryState
from .regions import REGIONS

DELAY_S_DEFAULT = 0.3
WORKERS_DEFAULT = 8

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "navrh" / "data" / "history"
OUT_DIR = REPO_ROOT / "navrh" / "output"

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    # tisk z víc vláken najednou by se bez zámku prokládal
    with _print_lock:
        print(msg, flush=True)


def _process_obec(
    kod_obec: str,
    nazev: str,
    months: list[str],
    session: requests.Session,
    listing_cache: MonthListingCache,
    delay_s: float,
) -> dict:
    """Zpracuje kompletní dostupnou historii jedné obce, sekvenčně měsíc po
    měsíci. Volá se z worker vlákna v `main` – souběžnost je na úrovni obcí,
    ne měsíců v rámci jedné obce."""
    state_path = DATA_DIR / f"{kod_obec}.parquet"
    state = HistoryState.load(state_path)
    already_done = set(state.processed_months)
    # měsíce, u kterých minule zdrojový soubor na serveru nebyl, v
    # `processed_months` nejsou -> jsou součástí `todo` a zkusí se znovu
    todo = [m for m in months if m not in already_done]

    ok_count, missing_count, error_count = 0, 0, 0
    for i, yyyymm in enumerate(todo, 1):
        result = fetch_obec_snapshot(yyyymm, kod_obec, session=session, listing_cache=listing_cache)
        if result.ok:
            state.ingest(result.gdf, yyyymm)
            ok_count += 1
        elif result.error == "soubor pro tento měsíc neexistuje":
            # neoznačujeme jako zpracované -> zkusí se znovu (soubor pro
            # poslední měsíce ČÚZK teprve zveřejní), viz mark_missing()
            state.mark_missing(yyyymm)
            missing_count += 1
        else:
            _log(f"  [{nazev} {yyyymm}] CHYBA: {result.error}")
            error_count += 1
            # neoznačujeme jako zpracované -> při dalším běhu se zkusí znovu

        if i % 24 == 0 or i == len(todo):
            state.save(state_path)

        if i < len(todo):
            time.sleep(delay_s)

    state.save(state_path)
    hist = state.to_geodataframe()
    _log(f"  hotovo {nazev} ({kod_obec}): {len(todo)} měsíců zpracováno "
         f"(ok={ok_count}, chybí={missing_count}, chyby={error_count}), "
         f"{len(hist)} řádků historie")
    return {
        "kod_obec": kod_obec,
        "nazev": nazev,
        "hist": hist,
        "ok": ok_count,
        "missing": missing_count,
        "errors": error_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default="brno_okoli", choices=sorted(REGIONS),
                         help="který pojmenovaný region zpracovat (viz regions.py)")
    parser.add_argument("--limit", type=int, default=None,
                         help="zpracovat jen prvních N obcí regionu (jsou řazené podle vzdálenosti od centra) – pro rychlý test")
    parser.add_argument("--workers", type=int, default=WORKERS_DEFAULT,
                         help=f"kolik obcí stahovat souběžně (výchozí {WORKERS_DEFAULT})")
    parser.add_argument("--delay", type=float, default=DELAY_S_DEFAULT,
                         help=f"pauza mezi requesty v rámci jedné obce, vteřiny (výchozí {DELAY_S_DEFAULT})")
    parser.add_argument("--start", default="201508", help="první měsíc RRRRMM (výchozí 201508)")
    parser.add_argument("--end", default=None, help="poslední měsíc RRRRMM (výchozí aktuální měsíc) – hodí se hlavně pro rychlý test na pár měsících")
    args = parser.parse_args()

    obce = REGIONS[args.region]
    if args.limit:
        obce = obce[: args.limit]

    months = month_range(start=args.start, end=args.end)
    _log(f"Region '{args.region}': {len(obce)} obcí, {len(months)} měsíců, "
         f"{args.workers} souběžných vláken, delay={args.delay}s/request")

    session = requests.Session()
    # výchozí pool_maxsize (10) by se souběžnými vlákny snadno vyčerpal a
    # připojení by se pak neznovupoužívala (tichá ztráta výhody keep-alive)
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=max(args.workers, 10), pool_maxsize=max(args.workers, 10)
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    listing_cache = MonthListingCache()

    t0 = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_process_obec, kod, nazev, months, session, listing_cache, args.delay): (kod, nazev)
            for kod, nazev, _dist in obce
        }
        for future in as_completed(futures):
            kod, nazev = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # jedna obec nesmí shodit celý běh
                _log(f"  [{nazev} {kod}] SELHALO: {exc}")

    elapsed = time.monotonic() - t0
    _log(f"\nHotovo za {elapsed / 60:.1f} min "
         f"({elapsed / max(len(obce), 1):.1f} s/obec průměrně).")

    if not results:
        _log("Žádná obec se úspěšně nezpracovala, konec.")
        sys.exit(1)

    # -- souhrnný export za celý region -------------------------------
    frames = [r["hist"].assign(obec_kod=r["kod_obec"], obec_nazev=r["nazev"])
              for r in results if len(r["hist"]) > 0]

    if not frames:
        _log("Žádná obec nemá žádná data, souhrnný export se přeskakuje.")
        return

    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    combined = combined.sort_values(["obec_kod", "code", "valid_from"]).reset_index(drop=True)

    name = f"{args.region}_history"
    write_geopackage(OUT_DIR / f"{name}.gpkg", combined)
    write_csv_and_geojson(combined, OUT_DIR, name,
                           keep_cols=["code", "obec_kod", "obec_nazev", "valid_from", "valid_to", "end_reason"])

    _log(f"\nCelkem: {len(combined)} řádků historie napříč {len(frames)} obcemi.")


if __name__ == "__main__":
    main()
