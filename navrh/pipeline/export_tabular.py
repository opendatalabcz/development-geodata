"""Export into a "flat" form for a quick overview of the data: a CSV with the
attributes only (for Excel/pandas) and a GeoJSON with the geometry only in
EPSG:4326 (for QGIS/geojson.io/Leaflet). The two are joined on `code`, in the
`building_objects` layer on `snapshot_date` as well.
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
    """Save only the geometry, plus the columns identifying each row, into a
    GeoJSON in EPSG:4326."""
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
