"""Export of the history into GeoParquet."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow as pa

from .vfr_parser import DATE_FIELDS


def write_geoparquet(gdf: gpd.GeoDataFrame, out_path: str | Path, quiet: bool = False) -> None:
    """Save the history into a GeoParquet file. Unlike the CSV/GeoJSON pair,
    attributes and geometry stay in one table, list-valued columns are kept as
    lists, date columns are stored as real dates (not timestamps) and the CRS is
    stored in the file metadata."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    gdf = gdf.copy()
    for col in DATE_FIELDS:
        if col in gdf.columns:
            gdf[col] = pd.to_datetime(gdf[col]).astype(pd.ArrowDtype(pa.date32()))

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    gdf.to_parquet(tmp_path, index=False)
    tmp_path.replace(out_path)

    if not quiet:
        print(f"Saved: {out_path}  ({len(gdf)} records, CRS {gdf.crs.to_string() if gdf.crs else None})")
