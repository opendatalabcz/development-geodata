"""Export of the history into GeoParquet."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd


def write_geoparquet(gdf: gpd.GeoDataFrame, out_path: str | Path) -> None:
    """Save the complete (versioned) history into a GeoParquet file. Unlike the
    CSV/GeoJSON pair, attributes and geometry stay in one table, list-valued
    columns are kept as lists and the CRS is stored in the file metadata."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    gdf.to_parquet(tmp_path, index=False)
    tmp_path.replace(out_path)

    print(f"Saved: {out_path}  ({len(gdf)} records, CRS {gdf.crs.to_string() if gdf.crs else None})")
