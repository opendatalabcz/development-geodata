"""PŘEDCHOZÍ ITERACE (viz navrh/pipeline/old) – nahrazeno history.py.

Demo spuštění na ukázkových souborech Chýně (2016 vs 2026): párové porovnání
dvou koncových snapshotů. Nahrazeno obecnějším a přesnějším přístupem
(SCD2 verzovaná historie přes všechny měsíční snapshoty, viz
navrh/pipeline/history.py + build_pilot_obec.py). Ponecháno pro referenci.

Použití (z kořene repozitáře):
    python -m navrh.pipeline.old.build_demo
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd

from .changefile import build_changefile, changefile_summary
from ..export_gpkg import write_geopackage
from ..export_tabular import write_csv_and_geojson
from ..vfr_parser import parse_building_objects

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
ANALYZA_DIR = REPO_ROOT / "analyza"
OUT_DIR = REPO_ROOT / "navrh" / "vystup" / "old"


def main() -> None:
    gdf_2016 = parse_building_objects(ANALYZA_DIR / "Chyne_UKSH_2016.xml")
    gdf_2026 = parse_building_objects(ANALYZA_DIR / "Chyne_UKSH_2026.xml")

    print(f"2016: {len(gdf_2016)} objektů, s polygonem: {gdf_2016['ma_polygon'].sum()}")
    print(f"2026: {len(gdf_2026)} objektů, s polygonem: {gdf_2026['ma_polygon'].sum()}")

    zmeny = build_changefile(gdf_2016, gdf_2026, key="Kod")
    print("\nPřehled změn 2016 -> 2026:")
    print(changefile_summary(zmeny))

    stavebni_objekty = gpd.GeoDataFrame(
        pd.concat([gdf_2016, gdf_2026], ignore_index=True), crs=gdf_2016.crs
    )

    write_geopackage(OUT_DIR / "chyne_zmeny.gpkg", stavebni_objekty, zmeny)

    write_csv_and_geojson(stavebni_objekty, OUT_DIR, "stavebni_objekty")
    write_csv_and_geojson(zmeny, OUT_DIR, "zmeny")


if __name__ == "__main__":
    main()
