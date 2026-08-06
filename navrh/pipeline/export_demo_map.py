"""Připraví odlehčený JSON podklad pro interaktivní demo mapu (Artifact) –
aktuálně platné stavební objekty obarvené podle roku dokončení, s popisy
kódů z číselníků ČÚZK.

Použití (z kořene repozitáře):
    python -m navrh.pipeline.export_demo_map
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ANALYZA_DIR = REPO_ROOT / "analyza"
VYSTUP_DIR = REPO_ROOT / "navrh" / "vystup"

LOOKUP_SOURCES = {
    "TypStavebnihoObjektuKod": ANALYZA_DIR / "CS_TYP_STAVEBNIHO_OBJEKTU.csv",
    "ZpusobVyuzitiKod": ANALYZA_DIR / "CE_ZPUSOB_VYUZITI_OBJEKTU.csv",
}


def _repair_cp1250(value):
    if not isinstance(value, str):
        return value
    try:
        return value.encode("latin1").decode("cp1250")
    except Exception:
        return value


def _load_lookup(path: Path, kod_col: str = "KOD", nazev_col: str = "NAZEV") -> dict:
    try:
        df = pd.read_csv(path, sep=";", encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, sep=";", encoding="latin1")
    df[kod_col] = df[kod_col].astype(str).str.strip()
    df[nazev_col] = df[nazev_col].astype(str).str.strip().apply(_repair_cp1250)
    df = df.drop_duplicates(subset=[kod_col], keep="first")
    return dict(zip(df[kod_col], df[nazev_col]))


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


def build_demo_map_json(historie_gpkg: Path, out_json: Path) -> None:
    hist = gpd.read_file(historie_gpkg, layer="stavebni_objekty")

    verze_pocet = hist.groupby("Kod").size().rename("pocet_verzi")
    aktualni = hist[hist["duvod_konce"].isna()].copy()
    aktualni = aktualni.merge(verze_pocet, on="Kod", how="left")

    for col, path in LOOKUP_SOURCES.items():
        if col not in aktualni.columns or not path.exists():
            continue
        aktualni[col] = aktualni[col].astype("string").str.strip()
        aktualni[f"{col}_popis"] = aktualni[col].map(_load_lookup(path))

    rok = pd.to_datetime(aktualni["Dokonceni"], errors="coerce").dt.year

    features = []
    for idx, row in aktualni.iterrows():
        g = _geom_to_coords(row.geometry)
        if g is None:
            continue
        r = rok.loc[idx]
        features.append({
            "kod": row["Kod"],
            "rok": None if pd.isna(r) else int(r),
            "plocha": None if pd.isna(row.get("ZastavenaPlocha")) else float(row["ZastavenaPlocha"]),
            "podlazi": None if pd.isna(row.get("PocetPodlazi")) else float(row["PocetPodlazi"]),
            "verzi": int(row["pocet_verzi"]),
            "typ": row.get("TypStavebnihoObjektuKod_popis") or None,
            "vyuziti": row.get("ZpusobVyuzitiKod_popis") or None,
            "geom": g,
        })

    out = {
        "bounds": list(hist.total_bounds),
        "n_aktualni": int(len(aktualni)),
        "n_chybi_rok": int(rok.isna().sum()),
        "rok_min": int(rok.min()),
        "rok_max": int(rok.max()),
        "features": features,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Uloženo: {out_json}  ({len(features)} objektů)")


if __name__ == "__main__":
    build_demo_map_json(VYSTUP_DIR / "539309_historie.gpkg", VYSTUP_DIR / "539309_dokonceni.json")
