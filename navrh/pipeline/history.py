"""Průběžné sestavování verzované historie stavebních objektů z posloupnosti
měsíčních snapshotů (SCD2 – slowly changing dimension).

Princip:
    - objekt beze změny mezi snapshoty -> žádný nový řádek
    - objekt, u kterého se něco změnilo (atribut nebo geometrie) -> stará
      verze se uzavře (`verze_do` = datum snapshotu, `duvod_konce="zmena"`)
      a otevře se nová (`verze_od` = datum snapshotu, `verze_do=None`)
    - nový objekt -> nová otevřená verze
    - objekt, který ze snapshotu zmizel -> poslední verze se uzavře
      (`duvod_konce="zanik"`)
    - `verze_do IS NULL` == objekt je platný k datu posledního zpracovaného snapshotu

Stav se drží v paměti (pro jednu obec řádově stovky až tisíce objektů, takže
to není problém) a dá se kdykoliv uložit/načíst přes GeoParquet – viz
`HistoryState.save` / `HistoryState.load` – takže zpracování jde kdykoliv
přerušit a navázat.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd

from .vfr_parser import CRS_VFR, VOLATILE_FIELDS

# sloupce, které nejsou "věcný atribut" stavebního objektu (řídicí/metadata sloupce)
_NON_ATTR_COLS = {
    "Kod", "_gml_id", "geometry", "definicni_bod", "ma_polygon",
    "snapshot_datum", "zdrojovy_soubor",
    "verze_od", "verze_do", "duvod_konce",
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
        self.open_by_kod: dict[str, int] = {} # Kod -> id aktuálně otevřeného řádku
        self._next_id = 0
        self.posledni_snapshot: pd.Timestamp | None = None
        self.zpracovane_mesice: list[str] = []
        self._attr_cols: list[str] | None = None

    # -- ingest ------------------------------------------------------------

    def ingest(self, gdf: gpd.GeoDataFrame, yyyymm: str | None = None) -> None:
        """Zapracuje jeden měsíční snapshot do stavu historie."""
        if len(gdf) == 0:
            if yyyymm:
                self.zpracovane_mesice.append(yyyymm)
            return

        snapshot_datum = gdf["snapshot_datum"].iloc[0]
        if self._attr_cols is None:
            self._attr_cols = [c for c in gdf.columns if c not in _NON_ATTR_COLS]
        attr_cols = self._attr_cols

        new_by_kod = {row["Kod"]: row for row in gdf.to_dict("records")}

        # zmizelé objekty -> uzavřít jako zanikle
        for kod in list(self.open_by_kod):
            if kod not in new_by_kod:
                self._close(kod, snapshot_datum, "zanik")

        # nové / změněné / beze změny
        for kod, new_row in new_by_kod.items():
            if kod not in self.open_by_kod:
                self._open(new_row, snapshot_datum)
                continue

            row_id = self.open_by_kod[kod]
            old_row = self.rows[row_id]
            if _row_differs(old_row, new_row, attr_cols):
                self._close(kod, snapshot_datum, "zmena")
                self._open(new_row, snapshot_datum)
            # jinak beze změny -> nic se neděje

        self.posledni_snapshot = snapshot_datum
        if yyyymm:
            self.zpracovane_mesice.append(yyyymm)

    def _open(self, row: dict, snapshot_datum) -> None:
        row = dict(row)
        row["verze_od"] = snapshot_datum
        row["verze_do"] = pd.NaT
        row["duvod_konce"] = None
        row_id = self._next_id
        self._next_id += 1
        self.rows[row_id] = row
        self.open_by_kod[row["Kod"]] = row_id

    def _close(self, kod: str, snapshot_datum, duvod: str) -> None:
        row_id = self.open_by_kod.pop(kod)
        self.rows[row_id]["verze_do"] = snapshot_datum
        self.rows[row_id]["duvod_konce"] = duvod

    # -- export / persist ----------------------------------------------------

    def to_geodataframe(self) -> gpd.GeoDataFrame:
        if not self.rows:
            return gpd.GeoDataFrame(columns=["Kod", "verze_od", "verze_do", "duvod_konce", "geometry"],
                                     geometry="geometry", crs=CRS_VFR)
        df = pd.DataFrame(list(self.rows.values()))
        gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=CRS_VFR)
        if "definicni_bod" in gdf.columns:
            # druhý (vedlejší) geometrický sloupec musí být taky opravdová
            # GeoSeries, jinak ho Parquet writer neumí serializovat
            gdf["definicni_bod"] = gpd.GeoSeries(gdf["definicni_bod"], crs=CRS_VFR)
        return gdf.sort_values(["Kod", "verze_od"]).reset_index(drop=True)

    def save(self, path: str | Path) -> None:
        """Uloží stav do GeoParquet (+ drobné metadata o postupu vedle)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        gdf = self.to_geodataframe()
        gdf.to_parquet(path)
        meta_path = path.with_suffix(".progress.json")
        import json
        meta_path.write_text(
            json.dumps({
                "zpracovane_mesice": self.zpracovane_mesice,
                "posledni_snapshot": str(self.posledni_snapshot) if self.posledni_snapshot is not None else None,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
            if pd.isna(row.get("verze_do")):
                state.open_by_kod[row["Kod"]] = row_id

        meta_path = path.with_suffix(".progress.json")
        if meta_path.exists():
            import json
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            state.zpracovane_mesice = meta.get("zpracovane_mesice", [])
            posl = meta.get("posledni_snapshot")
            state.posledni_snapshot = pd.to_datetime(posl) if posl else None

        return state
