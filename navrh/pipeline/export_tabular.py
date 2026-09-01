"""Export into a "flat" form for a quick overview of the data:

- CSV     - attributes only (no geometry), for a look in Excel/pandas
            (`df.describe()`, filtering, pivot tables...)
- GeoJSON - geometry only + the `code` key (and a few identifying attributes),
            in EPSG:4326 (WGS84), so it can be opened in
            QGIS/geojson.io/Leaflet

The two sets can be joined through the `code` column (for the
`building_objects` layer additionally through `snapshot_date`, because one
`code` appears in it twice - once for every processed snapshot).
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd

from ._util import flatten_for_output

GEOJSON_CRS = "EPSG:4326"


def write_csv(gdf: gpd.GeoDataFrame, out_path: str | Path) -> None:
    """Save the attribute table (without geometry) into a CSV (utf-8-sig for
    Excel's sake)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    df = flatten_for_output(gdf)
    df = df.drop(columns=["geometry"], errors="ignore")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"Saved: {out_path}  ({len(df)} rows, {len(df.columns)} columns)")


def write_geojson(
    gdf: gpd.GeoDataFrame,
    out_path: str | Path,
    keep_cols: list[str] | None = None,
) -> None:
    """Save only the geometry (+ identifying columns) into a GeoJSON in EPSG:4326.

    Parameters
    ----------
    keep_cols:
        which attribute columns the GeoJSON keeps alongside the geometry
        (default: `code` and `change_type`/`snapshot_date`, if present) - the
        remaining attributes are looked up by joining with the CSV on `code`.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if keep_cols is None:
        keep_cols = [c for c in ("code", "change_type", "snapshot_date") if c in gdf.columns]

    slim = gdf[[*keep_cols, "geometry"]].copy()
    slim = flatten_for_output(slim)
    slim = gpd.GeoDataFrame(slim, geometry="geometry", crs=gdf.crs).to_crs(GEOJSON_CRS)

    if out_path.exists():
        out_path.unlink()
    slim.to_file(out_path, driver="GeoJSON")
    print(f"Saved: {out_path}  ({len(slim)} objects, CRS {GEOJSON_CRS})")


def write_csv_and_geojson(
    gdf: gpd.GeoDataFrame,
    out_dir: str | Path,
    name: str,
    keep_cols: list[str] | None = None,
) -> None:
    """Save `<name>.csv` (attributes) and `<name>.geojson` (geometry)."""
    out_dir = Path(out_dir)
    write_csv(gdf, out_dir / f"{name}.csv")
    write_geojson(gdf, out_dir / f"{name}.geojson", keep_cols=keep_cols)
