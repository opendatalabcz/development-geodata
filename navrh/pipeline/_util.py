"""Shared helpers for the export modules."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd


def flatten_for_output(df):
    """Prepare attributes for formats that support neither list-valued
    columns nor more than one geometry column (CSV, GPKG, GeoJSON)."""
    df = df.copy()
    for col in df.columns:
        if col == "geometry":
            continue
        if isinstance(df[col].dtype, gpd.array.GeometryDtype):
            df[col] = gpd.GeoSeries(df[col]).apply(lambda g: g.wkt if g is not None else None).astype(object)
            continue
        if df[col].dtype == object and df[col].map(lambda v: isinstance(v, list)).any():
            df[col] = df[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            if getattr(df[col].dt, "tz", None) is not None:
                df[col] = df[col].dt.tz_localize(None)
    return df
