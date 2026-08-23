"""Export snapshotů do GeoPackage."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd

from ._util import flatten_for_output as _gpkg_safe


def write_geopackage(
    out_path: str | Path,
    stavebni_objekty: gpd.GeoDataFrame,
) -> None:
    """Uloží kompletní (verzovanou) historii do .gpkg (vrstva
    'building_objects'). Řádky se zavřenou verzí (`end_reason` vyplněný,
    tj. `valid_to IS NOT NULL`) jsou jen podmnožina téhle vrstvy – kdo je
    potřebuje zvlášť, vyfiltruje si je na místě, není důvod je duplikovat
    do vlastní vrstvy/souboru."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    so = _gpkg_safe(stavebni_objekty)

    if out_path.exists():
        out_path.unlink()

    so.to_file(out_path, layer="building_objects", driver="GPKG")

    print(f"Uloženo: {out_path}")
    print(f"  vrstva 'building_objects': {len(so)} záznamů")
