"""PŘEDCHOZÍ ITERACE (viz navrh/pipeline/old) – nahrazeno build_region.py.

Pilotní běh na jedné natvrdo zadané obci (Chýně, kód 539309) přes celou
dostupnou historii. Nahrazeno univerzálním `build_region.py`, který totéž
umí pro libovolnou obec i libovolný rozsah měsíců a navíc kontroluje, že
zadaný rozsah na dosavadní stav vůbec navazuje:

    python -m navrh.pipeline.build_region --obec 539309

Ponecháno pro referenci.

Použití (z kořene repozitáře):
    python -m navrh.pipeline.old.build_pilot_obec
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..download import MISSING_FILE_ERROR, fetch_municipality_snapshot, month_range
from ..export_gpkg import write_geopackage
from ..export_tabular import write_csv_and_geojson
from ..history import HistoryState

KOD_OBEC = "539309"  # Chýně
DELAY_S = 1.0

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = REPO_ROOT / "navrh" / "data" / "history"
OUT_DIR = REPO_ROOT / "navrh" / "output"
STATE_PATH = DATA_DIR / f"{KOD_OBEC}.parquet"


def main() -> None:
    months = month_range()  # 201508 .. aktuální měsíc

    state = HistoryState.load(STATE_PATH)
    already_done = set(state.processed_months)
    todo = [m for m in months if m not in already_done]
    # měsíce, u kterých minule zdrojový soubor na serveru nebyl, v
    # `processed_months` nejsou -> jsou součástí `todo` a zkusí se znovu
    todo = [m for m in todo if m not in set(state.skipped_months)]
    retry = [m for m in todo if m in set(state.pending_months)]

    print(f"Obec {KOD_OBEC}: {len(months)} měsíců celkem, "
          f"{len(already_done)} už zpracováno, {len(todo)} zbývá "
          f"(z toho {len(retry)} opakovaný pokus o dřív chybějící soubor).")

    ok_count, missing_count, error_count = 0, 0, 0
    for i, yyyymm in enumerate(todo, 1):
        result = fetch_municipality_snapshot(yyyymm, KOD_OBEC)
        if result.ok:
            state.ingest(result.gdf, yyyymm)
            ok_count += 1
        else:
            # zkusit znovu má smysl jen u měsíců, které ČÚZK teprve
            # zveřejní, viz mark_failed()
            state.mark_failed(yyyymm, months[-1])
            if result.error == MISSING_FILE_ERROR:
                missing_count += 1
            else:
                print(f"  [{yyyymm}] CHYBA: {result.error}", file=sys.stderr)
                error_count += 1

        if i % 12 == 0 or i == len(todo):
            state.save(STATE_PATH)
            print(f"  ...{i}/{len(todo)} zpracováno "
                  f"(ok={ok_count}, chybí={missing_count}, chyby={error_count}) "
                  f"– průběžně uloženo do {STATE_PATH.name}")

        if i < len(todo):
            import time
            time.sleep(DELAY_S)

    state.save(STATE_PATH)

    hist = state.to_geodataframe()
    print(f"\nHotovo. Historie: {len(hist)} řádků, {hist['code'].nunique()} unikátních objektů.")
    print(hist["end_reason"].value_counts(dropna=False))

    write_geopackage(OUT_DIR / f"{KOD_OBEC}_history.gpkg", hist)
    write_csv_and_geojson(hist, OUT_DIR, f"{KOD_OBEC}_history",
                           keep_cols=["code", "valid_from", "valid_to", "end_reason"])


if __name__ == "__main__":
    main()
