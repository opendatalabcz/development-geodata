"""Pilotní běh: stáhne a zpracuje CELOU dostupnou měsíční historii jedné
obce (výchozí: Chýně, kód 539309) ze services.cuzk.gov.cz/vfr a sestaví
verzovanou historii stavebních objektů.

Nic se neukládá na disk kromě výsledků – XML se stahuje, parsuje a
zahazuje po jednom měsíci (viz download.py). Stav historie se průběžně
ukládá do GeoParquet, takže běh jde kdykoliv přerušit a znovu spustit –
už zpracované měsíce se nestahují znovu.

Použití (z kořene repozitáře):
    python -m navrh.pipeline.build_pilot_obec
"""

from __future__ import annotations

import sys
from pathlib import Path

from .download import fetch_obec_snapshot, month_range
from .export_gpkg import write_geopackage
from .export_tabular import write_csv_and_geojson
from .history import HistoryState

KOD_OBEC = "539309"  # Chýně
DELAY_S = 1.0

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
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
    retry = [m for m in todo if m in set(state.missing_months)]

    print(f"Obec {KOD_OBEC}: {len(months)} měsíců celkem, "
          f"{len(already_done)} už zpracováno, {len(todo)} zbývá "
          f"(z toho {len(retry)} opakovaný pokus o dřív chybějící soubor).")

    ok_count, missing_count, error_count = 0, 0, 0
    for i, yyyymm in enumerate(todo, 1):
        result = fetch_obec_snapshot(yyyymm, KOD_OBEC)
        if result.ok:
            state.ingest(result.gdf, yyyymm)
            ok_count += 1
        elif result.error == "soubor pro tento měsíc neexistuje":
            # neoznačujeme jako zpracované -> zkusí se znovu (soubor pro
            # poslední měsíce ČÚZK teprve zveřejní), viz mark_missing()
            state.mark_missing(yyyymm)
            missing_count += 1
        else:
            print(f"  [{yyyymm}] CHYBA: {result.error}", file=sys.stderr)
            error_count += 1
            # neoznačujeme jako zpracované -> při dalším běhu se zkusí znovu

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
