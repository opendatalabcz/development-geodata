"""Export snapshotů a změnového souboru do jednoho GeoPackage (víc vrstev)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd

from ._util import flatten_for_output as _gpkg_safe


def write_geopackage(
    out_path: str | Path,
    stavebni_objekty: gpd.GeoDataFrame,
    zmeny: gpd.GeoDataFrame,
) -> None:
    """Uloží kompletní snapshoty i changefile do jednoho .gpkg (2 vrstvy)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    so = _gpkg_safe(stavebni_objekty)
    zm = _gpkg_safe(zmeny)

    # geopandas/pyogrio neumí zapsat do stejného souboru přes dvě různé volání
    # se stejným jménem vrstvy, proto při existenci starého souboru smažeme
    if out_path.exists():
        out_path.unlink()

    so.to_file(out_path, layer="building_objects", driver="GPKG")
    zm.to_file(out_path, layer="changes", driver="GPKG")

    print(f"Uloženo: {out_path}")
    print(f"  vrstva 'building_objects': {len(so)} záznamů")
    print(f"  vrstva 'changes':          {len(zm)} záznamů")
