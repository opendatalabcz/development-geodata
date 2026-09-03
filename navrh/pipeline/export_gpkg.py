"""Export of snapshots into GeoPackage."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd

from ._util import flatten_for_output as _gpkg_safe


def write_geopackage(
    out_path: str | Path,
    building_objects: gpd.GeoDataFrame,
) -> None:
    """Save the complete (versioned) history into a .gpkg (layer
    'building_objects')."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    gdf = _gpkg_safe(building_objects)

    if out_path.exists():
        out_path.unlink()

    gdf.to_file(out_path, layer="building_objects", driver="GPKG")

    print(f"Saved: {out_path}")
    print(f"  layer 'building_objects': {len(gdf)} records")
