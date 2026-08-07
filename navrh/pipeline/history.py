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
        self._attr_cols: list[str] | None = None

    # -- ingest ------------------------------------------------------------

    def ingest(self, gdf: gpd.GeoDataFrame, yyyymm: str | None = None) -> None:
        """Zapracuje jeden měsíční snapshot do stavu historie."""
        if len(gdf) == 0:
            if yyyymm:
                self.processed_months.append(yyyymm)
            return

        snapshot_date = gdf["snapshot_date"].iloc[0]
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
        if yyyymm:
            self.processed_months.append(yyyymm)

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
        """Uloží stav do GeoParquet (+ drobné metadata o postupu vedle)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        gdf = self.to_geodataframe()
        gdf.to_parquet(path)
        meta_path = path.with_suffix(".progress.json")
        import json
        meta_path.write_text(
            json.dumps({
                "processed_months": self.processed_months,
                "last_snapshot": str(self.last_snapshot) if self.last_snapshot is not None else None,
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
            if pd.isna(row.get("valid_to")):
                state.open_by_code[row["code"]] = row_id

        meta_path = path.with_suffix(".progress.json")
        if meta_path.exists():
            import json
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            state.processed_months = meta.get("processed_months", [])
            last = meta.get("last_snapshot")
            state.last_snapshot = pd.to_datetime(last) if last else None

        return state
