"""Průběžné sestavování verzované historie stavebních objektů z posloupnosti
měsíčních snapshotů (SCD2 – slowly changing dimension).

Princip:
    - objekt beze změny mezi snapshoty -> žádný nový řádek
    - objekt, u kterého se něco změnilo (atribut nebo geometrie) -> stará
      verze se uzavře (`valid_to` = datum snapshotu, `end_reason="change"`)
      a otevře se nová (`valid_from` = datum snapshotu, `valid_to=None`)
    - nový objekt -> nová otevřená verze
    - objekt, který ze snapshotu zmizel -> poslední verze se uzavře
      (`end_reason="removed"`)
    - `valid_to IS NULL` == objekt je platný k datu posledního zpracovaného snapshotu

Stav se drží v paměti (pro jednu obec řádově stovky až tisíce objektů, takže
to není problém) a dá se kdykoliv uložit/načíst přes GeoParquet – viz
`HistoryState.save` / `HistoryState.load` – takže zpracování jde kdykoliv
přerušit a navázat.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .vfr_parser import CRS_VFR, VOLATILE_FIELDS

# sloupce, které nejsou "věcný atribut" stavebního objektu (řídicí/metadata sloupce)
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
    """Drží průběžný stav verzované historie stavebních objektů jedné obce."""

    def __init__(self):
        self.rows: dict[int, dict] = {}       # id řádku -> data řádku (otevřené i uzavřené)
        self.open_by_code: dict[str, int] = {} # code -> id aktuálně otevřeného řádku
        self._next_id = 0
        self.last_snapshot: pd.Timestamp | None = None
        self.processed_months: list[str] = []
        self.missing_months: list[str] = []
        self._attr_cols: list[str] | None = None

    # -- evidence zpracovaných / chybějících měsíců ------------------------

    def _mark_processed(self, yyyymm: str | None) -> None:
        if not yyyymm:
            return
        if yyyymm not in self.processed_months:
            self.processed_months.append(yyyymm)
        if yyyymm in self.missing_months:
            self.missing_months.remove(yyyymm)

    def mark_missing(self, yyyymm: str) -> None:
        """Zaznamená měsíc, pro který zdrojový soubor na serveru (zatím) není.

        Záměrně se NEpřidává do `processed_months`: chybějící soubor není
        konečný stav. Typicky jde o poslední měsíc rozsahu, který ČÚZK
        ještě nezveřejnil – při příštím běhu už tam být může. Kdyby se
        takový měsíc rovnou označil za zpracovaný, `build_*.py` by ho už
        nikdy nezkusily znovu a snapshot by v historii natrvalo chyběl.
        Měsíce z `missing_months` se proto zkoušejí při každém běhu znovu
        (stojí to jen výpis adresáře, viz `MonthListingCache`)."""
        if yyyymm not in self.missing_months:
            self.missing_months.append(yyyymm)

    # -- ingest ------------------------------------------------------------

    def ingest(self, gdf: gpd.GeoDataFrame, yyyymm: str | None = None) -> None:
        """Zapracuje jeden měsíční snapshot do stavu historie.

        Musí být bezpečné zavolat dvakrát za sebou se stejným měsícem beze
        změny výsledku (idempotentní) – po pádu procesu a resumu (viz
        `build_region.py`) se totiž může stát, že `.progress.json` neodpovídá
        přesně tomu, co je už reálně uložené v `.parquet` (`save()` píše obě
        věci jako dva oddělené, ne-atomické zápisy), takže stejný měsíc se
        může pokusit zpracovat podruhé."""
        if yyyymm is not None and yyyymm in self.processed_months:
            return

        if len(gdf) == 0:
            self._mark_processed(yyyymm)
            return

        snapshot_date = gdf["snapshot_date"].iloc[0]
        if self.last_snapshot is not None and snapshot_date <= self.last_snapshot:
            # stejná obrana podle data snapshotu, pro případ že by
            # `processed_months` ze staršího/nekonzistentního stavu tenhle
            # měsíc neobsahoval, ale data z něj už promítnutá byla
            self._mark_processed(yyyymm)
            return

        if self._attr_cols is None:
            self._attr_cols = [c for c in gdf.columns if c not in _NON_ATTR_COLS]
        attr_cols = self._attr_cols

        new_by_code = {row["code"]: row for row in gdf.to_dict("records")}

        # zmizelé objekty -> uzavřít jako zanikle
        for code in list(self.open_by_code):
            if code not in new_by_code:
                self._close(code, snapshot_date, "removed")

        # nové / změněné / beze změny
        for code, new_row in new_by_code.items():
            if code not in self.open_by_code:
                self._open(new_row, snapshot_date)
                continue

            row_id = self.open_by_code[code]
            old_row = self.rows[row_id]
            if _row_differs(old_row, new_row, attr_cols):
                self._close(code, snapshot_date, "change")
                self._open(new_row, snapshot_date)
            # jinak beze změny -> nic se neděje

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
            # druhý (vedlejší) geometrický sloupec musí být taky opravdová
            # GeoSeries, jinak ho Parquet writer neumí serializovat
            gdf["reference_point"] = gpd.GeoSeries(gdf["reference_point"], crs=CRS_VFR)
        return gdf.sort_values(["code", "valid_from"]).reset_index(drop=True)

    def save(self, path: str | Path) -> None:
        """Uloží stav do GeoParquet (+ drobné metadata o postupu vedle).

        Oba soubory se zapisují atomicky (přes dočasný soubor + `os.replace`,
        což je na stejném svazku atomické přejmenování) – nemůže tak vzniknout
        napůl zapsaný/useknutý soubor, kdyby proces spadl uprostřed zápisu.
        `.parquet` se navíc zapíše vždy jako první a `.progress.json` až po
        něm, takže i při pádu přesně mezi zápisem obou souborů zůstává
        `.progress.json` nanejvýš pozadu za `.parquet` (nikdy napřed) – a to
        `ingest()` umí bezpečně dohnat (viz tam)."""
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
                "missing_months": self.missing_months,
                "last_snapshot": str(self.last_snapshot) if self.last_snapshot is not None else None,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(meta_tmp, meta_path)

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
        legacy_meta = False
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            state.processed_months = meta.get("processed_months", [])
            state.missing_months = meta.get("missing_months", [])
            legacy_meta = "missing_months" not in meta
            last = meta.get("last_snapshot")
            state.last_snapshot = pd.to_datetime(last) if last else None

        # obrana proti tomu, že `.progress.json` je i po atomickém zápisu
        # starší než `.parquet` (viz `save()`) – co je vidět přímo v datech
        # (valid_from/valid_to už zpracovaných řádků) bereme jako pravdu,
        # ať `ingest()` znovu neotvírá měsíce, které v datech už jsou
        if state.rows:
            seen_dates = [r["valid_from"] for r in state.rows.values() if pd.notna(r.get("valid_from"))]
            seen_dates += [r["valid_to"] for r in state.rows.values() if pd.notna(r.get("valid_to"))]
            if seen_dates:
                data_last_snapshot = max(seen_dates)
                if state.last_snapshot is None or data_last_snapshot > state.last_snapshot:
                    state.last_snapshot = data_last_snapshot

        if legacy_meta:
            state._migrate_legacy_missing_months()

        return state

    def _migrate_legacy_missing_months(self) -> None:
        """Oprava stavů uložených starší verzí, která chybějící měsíce
        zapisovala rovnou do `processed_months` (a tím je natrvalo umrtvila).

        Rozlišit zpětně "zpracováno, nic se nezměnilo" od "soubor nebyl"
        nejde – v `processed_months` po sobě obojí nechává stejnou stopu.
        Jistotu ale máme u měsíců *za* posledním skutečně zpracovaným
        snapshotem: do těch se `ingest()` prokazatelně nedostal (jinak by
        `last_snapshot` byl dál), takže šlo o chybějící soubory. A právě ty
        jsou ten problematický případ – konec rozsahu, který ČÚZK v době
        běhu ještě nezveřejnil. Odebráním z `processed_months` se při
        dalším běhu zkusí znovu.

        Případné chybějící měsíce uvnitř řady zůstanou označené jako
        zpracované; ČÚZK starší měsíce zpětně nedoplňuje, takže by stejně
        nic nepřinesly."""
        if not self.processed_months:
            return
        if self.last_snapshot is None:
            stuck = set(self.processed_months)
        else:
            # soubor `RRRRMMDD` leží v adresáři svého měsíce a jeho
            # snapshot_date (PlatiOd) je až následující den, viz vfr_parser
            last_dir = (self.last_snapshot - pd.Timedelta(days=1)).strftime("%Y%m")
            stuck = {m for m in self.processed_months if m > last_dir}
        if stuck:
            self.processed_months = [m for m in self.processed_months if m not in stuck]
