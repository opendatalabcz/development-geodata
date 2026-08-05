"""Export do „ploché" podoby pro rychlý přehled nad daty:

- CSV  – čistě atributy (bez geometrie), pro pohled v Excelu/pandas
         (`df.describe()`, filtrování, pivotky…)
- GeoJSON – čistě geometrie + klíč `Kod` (a pár identifikačních atributů),
         v EPSG:4326 (WGS84), aby šlo otevřít v QGIS/geojson.io/Leafletu

Obě sady jde propojit přes sloupec `Kod` (u vrstvy `stavebni_objekty` navíc
přes `snapshot_datum`, protože jeden `Kod` se v ní objevuje 2× – jednou pro
každý zpracovaný snapshot).
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd

from ._util import flatten_for_output

GEOJSON_CRS = "EPSG:4326"


def write_csv(gdf: gpd.GeoDataFrame, out_path: str | Path) -> None:
    """Uloží atributovou tabulku (bez geometrie) do CSV (utf-8-sig kvůli Excelu)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    df = flatten_for_output(gdf)
    df = df.drop(columns=["geometry"], errors="ignore")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"Uloženo: {out_path}  ({len(df)} řádků, {len(df.columns)} sloupců)")


def write_geojson(
    gdf: gpd.GeoDataFrame,
    out_path: str | Path,
    keep_cols: list[str] | None = None,
) -> None:
    """Uloží jen geometrii (+ identifikační sloupce) do GeoJSON v EPSG:4326.

    Parameters
    ----------
    keep_cols:
        které atributové sloupce si GeoJSON ponechá vedle geometrie (výchozí:
        `Kod` a `typ_zmeny`/`snapshot_datum`, pokud existují) – zbytek atributů
        se dohledá spojením s CSV přes `Kod`.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if keep_cols is None:
        keep_cols = [c for c in ("Kod", "typ_zmeny", "snapshot_datum") if c in gdf.columns]

    slim = gdf[[*keep_cols, "geometry"]].copy()
    slim = flatten_for_output(slim)
    slim = gpd.GeoDataFrame(slim, geometry="geometry", crs=gdf.crs).to_crs(GEOJSON_CRS)

    if out_path.exists():
        out_path.unlink()
    slim.to_file(out_path, driver="GeoJSON")
    print(f"Uloženo: {out_path}  ({len(slim)} objektů, CRS {GEOJSON_CRS})")


def write_csv_and_geojson(
    gdf: gpd.GeoDataFrame,
    out_dir: str | Path,
    name: str,
    keep_cols: list[str] | None = None,
) -> None:
    """Pohodlná zkratka: uloží `<name>.csv` (atributy) a `<name>.geojson` (geometrie)."""
    out_dir = Path(out_dir)
    write_csv(gdf, out_dir / f"{name}.csv")
    write_geojson(gdf, out_dir / f"{name}.geojson", keep_cols=keep_cols)
