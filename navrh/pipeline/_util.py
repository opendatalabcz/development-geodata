"""Shared helpers for the export modules."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd


def flatten_for_output(df):
    """Prepare attributes for formats that support neither list-valued columns
    nor more than one geometry column (CSV, GPKG, GeoJSON) - lists (e.g.
    `changed_fields`) are joined into comma-separated text, secondary geometry
    columns (e.g. `reference_point`) are converted to WKT text and timezones
    are stripped from datetime columns."""
    df = df.copy()
    for col in df.columns:
        if col == "geometry":
            continue
        if isinstance(df[col].dtype, gpd.array.GeometryDtype):
            # .astype(object): on an empty (0-row) df `.apply()` has nothing to
            # infer the result type from and keeps the "geometry" dtype as is
            df[col] = gpd.GeoSeries(df[col]).apply(lambda g: g.wkt if g is not None else None).astype(object)
            continue
        # only object columns can hold a Python list at all - on typed columns
        # (datetime64 etc.) this test blows up on newer pandas
        if df[col].dtype == object and df[col].map(lambda v: isinstance(v, list)).any():
            df[col] = df[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            if getattr(df[col].dt, "tz", None) is not None:
                df[col] = df[col].dt.tz_localize(None)
    return df
