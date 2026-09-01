"""DEMO ONLY (see navrh/pipeline/demo) - not part of the main pipeline.

Prepares a lightweight JSON base for the interactive demo map (Artifact) -
currently valid building objects coloured by their completion year, with labels
for the codes taken from the ČÚZK code lists.

Reads the current output of the main pipeline
(navrh/output/539309_history.gpkg), the result is stored separately in
navrh/output/demo/.

Usage (from the repository root):
    python -m navrh.pipeline.demo.export_demo_map
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
ANALYSIS_DIR = REPO_ROOT / "analyza"
CODELISTS_DIR = ANALYSIS_DIR / "ciselniky"
OUTPUT_DIR = REPO_ROOT / "navrh" / "output"
DEMO_OUT_DIR = OUTPUT_DIR / "demo"

LOOKUP_SOURCES = {
    "building_type_code": CODELISTS_DIR / "CS_TYP_STAVEBNIHO_OBJEKTU.csv",
    "usage_type_code": CODELISTS_DIR / "CE_ZPUSOB_VYUZITI_OBJEKTU.csv",
}


def _repair_cp1250(value):
    if not isinstance(value, str):
        return value
    try:
        return value.encode("latin1").decode("cp1250")
    except Exception:
        return value


def _load_lookup(path: Path, code_col: str = "KOD", name_col: str = "NAZEV") -> dict:
    try:
        df = pd.read_csv(path, sep=";", encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, sep=";", encoding="latin1")
    df[code_col] = df[code_col].astype(str).str.strip()
    df[name_col] = df[name_col].astype(str).str.strip().apply(_repair_cp1250)
    df = df.drop_duplicates(subset=[code_col], keep="first")
    return dict(zip(df[code_col], df[name_col]))


def _geom_to_coords(geom, precision: int = 1):
    if geom is None:
        return None
    gt = geom.geom_type
    if gt == "Point":
        return {"type": "Point", "coords": [round(geom.x, precision), round(geom.y, precision)]}
    if gt == "Polygon":
        rings = [[[round(x, precision), round(y, precision)] for x, y in ring.coords]
                 for ring in [geom.exterior] + list(geom.interiors)]
        return {"type": "Polygon", "coords": rings}
    if gt == "MultiPolygon":
        polys = []
        for poly in geom.geoms:
            rings = [[[round(x, precision), round(y, precision)] for x, y in ring.coords]
                     for ring in [poly.exterior] + list(poly.interiors)]
            polys.append(rings)
        return {"type": "MultiPolygon", "coords": polys}
    return None


def build_demo_map_json(history_gpkg: Path, out_json: Path) -> None:
    hist = gpd.read_file(history_gpkg, layer="building_objects")

    version_count = hist.groupby("code").size().rename("version_count")
    current = hist[hist["end_reason"].isna()].copy()
    current = current.merge(version_count, on="code", how="left")

    for col, path in LOOKUP_SOURCES.items():
        if col not in current.columns or not path.exists():
            continue
        current[col] = current[col].astype("string").str.strip()
        current[f"{col}_label"] = current[col].map(_load_lookup(path))

    year = pd.to_datetime(current["completion_date"], errors="coerce").dt.year

    features = []
    for idx, row in current.iterrows():
        g = _geom_to_coords(row.geometry)
        if g is None:
            continue
        y = year.loc[idx]
        features.append({
            "code": row["code"],
            "year": None if pd.isna(y) else int(y),
            "area": None if pd.isna(row.get("built_up_area")) else float(row["built_up_area"]),
            "floors": None if pd.isna(row.get("floor_count")) else float(row["floor_count"]),
            "versions": int(row["version_count"]),
            "type": row.get("building_type_code_label") or None,
            "usage": row.get("usage_type_code_label") or None,
            "geom": g,
        })

    out = {
        "bounds": list(hist.total_bounds),
        "current_count": int(len(current)),
        "missing_year_count": int(year.isna().sum()),
        "year_min": int(year.min()),
        "year_max": int(year.max()),
        "features": features,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Saved: {out_json}  ({len(features)} objects)")


if __name__ == "__main__":
    build_demo_map_json(OUTPUT_DIR / "539309_history.gpkg", DEMO_OUT_DIR / "539309_completion.json")
