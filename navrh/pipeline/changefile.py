"""Porovnání dvou po sobě jdoucích snapshotů stavebních objektů a sestavení
změnového souboru (changefile).

Princip: RÚIAN VFR "změnové soubory" obsahují celý prvek, pokud se změnil
byť jediný atribut. Tady děláme to samé, ale sami – porovnáním dvou úplných
měsíčních stavů (OB_UKSH) podle stabilního klíče `Kod`:

    - "novy"     – Kod je jen v novějším snapshotu (vznikl / začal se sledovat)
    - "zanikly"  – Kod je jen ve starším snapshotu (zanikl / vypadl ze sady)
    - "zmenen"   – Kod je v obou, ale liší se aspoň jeden atribut nebo geometrie
    - "beze_zmeny" – beze změny (do finálního changefile se běžně nezahrnuje)
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd

from .vfr_parser import VOLATILE_FIELDS

# sloupce, které do porovnání "věcné" změny nevstupují
_NON_ATTR_COLS = {
    "Kod", "_gml_id", "geometry", "definicni_bod", "ma_polygon",
    "snapshot_datum", "zdrojovy_soubor", *VOLATILE_FIELDS,
}


def _geoms_equal(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return a.equals(b)
    except Exception:
        return False


def build_changefile(
    gdf_pred: gpd.GeoDataFrame,
    gdf_po: gpd.GeoDataFrame,
    key: str = "Kod",
    include_unchanged: bool = False,
) -> gpd.GeoDataFrame:
    """Sestaví changefile mezi dvěma snapshoty.

    Vrací GeoDataFrame s geometrií "po" (u zaniklých objektů geometrií "pred"),
    sloupcem `typ_zmeny` a `zmenena_pole` (seznam názvů atributů, které se liší).
    """
    datum_pred = gdf_pred["snapshot_datum"].iloc[0] if len(gdf_pred) else None
    datum_po = gdf_po["snapshot_datum"].iloc[0] if len(gdf_po) else None

    attr_cols = [c for c in gdf_pred.columns if c not in _NON_ATTR_COLS]

    pred = pd.DataFrame(gdf_pred.drop(columns="geometry")).set_index(key, drop=False)
    po = pd.DataFrame(gdf_po.drop(columns="geometry")).set_index(key, drop=False)
    geom_pred = gdf_pred.set_index(key, drop=False)["geometry"]
    geom_po = gdf_po.set_index(key, drop=False)["geometry"]

    all_keys = pred.index.union(po.index)

    rows = []
    for k in all_keys:
        in_pred = k in pred.index
        in_po = k in po.index

        if in_pred and not in_po:
            rows.append({key: k, "typ_zmeny": "zanikly", "zmenena_pole": None,
                         "geometry": geom_pred.loc[k], **pred.loc[k, attr_cols].to_dict()})
            continue

        if in_po and not in_pred:
            rows.append({key: k, "typ_zmeny": "novy", "zmenena_pole": None,
                         "geometry": geom_po.loc[k], **po.loc[k, attr_cols].to_dict()})
            continue

        row_pred = pred.loc[k, attr_cols]
        row_po = po.loc[k, attr_cols]
        zmenena = [
            c for c in attr_cols
            if not (pd.isna(row_pred[c]) and pd.isna(row_po[c])) and row_pred[c] != row_po[c]
        ]
        geom_changed = not _geoms_equal(geom_pred.loc[k], geom_po.loc[k])
        if geom_changed:
            zmenena.append("geometry")

        if zmenena:
            rows.append({key: k, "typ_zmeny": "zmenen", "zmenena_pole": zmenena,
                         "geometry": geom_po.loc[k], **row_po.to_dict()})
        elif include_unchanged:
            rows.append({key: k, "typ_zmeny": "beze_zmeny", "zmenena_pole": None,
                         "geometry": geom_po.loc[k], **row_po.to_dict()})

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=gdf_po.crs)
    out["datum_pred"] = datum_pred
    out["datum_po"] = datum_po
    return out


def changefile_summary(changefile: gpd.GeoDataFrame) -> pd.Series:
    """Rychlý přehled počtu změn podle typu."""
    return changefile["typ_zmeny"].value_counts()
