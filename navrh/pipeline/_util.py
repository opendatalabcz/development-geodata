"""Sdílené pomocné funkce pro export."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd


def flatten_for_output(df):
    """Připraví atributy na zápis do formátů, které neumí sloupce se seznamy
    ani víc než jednu geometrii (CSV, GPKG, GeoJSON) – seznamy (např.
    `changed_fields`) spojí do textu odděleného čárkou, vedlejší geometrické
    sloupce (např. `reference_point`) převede na WKT text a odstraní timezone
    z datumových sloupců."""
    df = df.copy()
    for col in df.columns:
        if col == "geometry":
            continue
        if isinstance(df[col].dtype, gpd.array.GeometryDtype):
            # .astype(object): na prázdném (0řádkovém) df `.apply()` nemá z čeho
            # odvodit výsledný typ a vrátí beze změny pořád "geometry" dtype
            df[col] = gpd.GeoSeries(df[col]).apply(lambda g: g.wkt if g is not None else None).astype(object)
            continue
        # jen object sloupce mohou vůbec obsahovat Python list – na typovaných
        # sloupcích (datetime64 apod.) tenhle test na novějším pandasu spadne
        if df[col].dtype == object and df[col].map(lambda v: isinstance(v, list)).any():
            df[col] = df[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            if getattr(df[col].dt, "tz", None) is not None:
                df[col] = df[col].dt.tz_localize(None)
    return df
