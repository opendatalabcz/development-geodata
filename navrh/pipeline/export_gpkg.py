"""Export snapshotů a změnového souboru do jednoho GeoPackage (víc vrstev)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd


def _gpkg_safe(df: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Připraví GeoDataFrame na zápis do GeoPackage (bez seznamů/objektů v buňkách)."""
    df = df.copy()
    for col in df.columns:
        if col == "geometry":
            continue
        if df[col].map(lambda v: isinstance(v, list)).any():
            df[col] = df[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            # GeoPackage/OGR datetime chce naivní nebo ISO string; necháme datetime,
            # pyogrio si s tím poradí, jen odstraníme případnou timezone
            if getattr(df[col].dt, "tz", None) is not None:
                df[col] = df[col].dt.tz_localize(None)
    return df


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

    so.to_file(out_path, layer="stavebni_objekty", driver="GPKG")
    zm.to_file(out_path, layer="zmeny", driver="GPKG")

    print(f"Uloženo: {out_path}")
    print(f"  vrstva 'stavebni_objekty': {len(so)} záznamů")
    print(f"  vrstva 'zmeny':            {len(zm)} záznamů")
