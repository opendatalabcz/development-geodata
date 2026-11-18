"""Change events between consecutive monthly snapshots of one municipality, and
the SCD2 history (valid_from / valid_to / end_reason) derived from them. See
month_events_spec.md for the design.

    - object new in the snapshot            -> `added`   (carries the new row)
    - object differing from its predecessor -> `changed` (carries the new row)
    - object missing from the snapshot      -> `removed` (carries the last known row)
    - unchanged object                      -> no event
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from .history import _NON_ATTR_COLS, _geoms_equal
from .vfr_parser import CRS_VFR, LIST_FIELDS

ADDED, CHANGED, REMOVED = "added", "changed", "removed"

# columns the events add in front of the record columns
EVENT_COLUMNS = ["municipality_code", "yyyymm", "event_date", "event_type"]

_END_REASON = {CHANGED: "change", REMOVED: "removed"}


def _list_key(values: pd.Series) -> pd.Series:
    """Lists become comparable strings (order matters, as with list equality)."""
    return values.map(lambda v: "\x1f".join(map(str, v)) if isinstance(v, list) else None)


def _column_differs(old: pd.Series, new: pd.Series) -> np.ndarray:
    """Per row: True when exactly one of the values is missing, or both are
    present and unequal."""
    old_na, new_na = old.isna().to_numpy(), new.isna().to_numpy()
    both = ~old_na & ~new_na
    equal = np.zeros(len(old), dtype=bool)
    if both.any():
        equal[both] = np.asarray(old[both].to_numpy() == new[both].to_numpy(), dtype=bool)
    return ~((old_na & new_na) | equal)


def _geometry_differs(old: pd.Series, new: pd.Series) -> np.ndarray:
    """Per row: True when the geometries differ. Exactly equal ones are settled
    in one vectorized call; only the rest go through the topological
    `_geoms_equal` that the history has always used."""
    old, new = old.to_numpy(), new.to_numpy()
    old_na, new_na = shapely.is_missing(old), shapely.is_missing(new)
    differs = old_na != new_na
    both = ~old_na & ~new_na
    exact = np.zeros(len(old), dtype=bool)
    if both.any():
        exact[both] = shapely.equals_exact(old[both], new[both], tolerance=0)
    for i in np.flatnonzero(both & ~exact):
        differs[i] = not _geoms_equal(old[i], new[i])
    return differs


def _rows_differ(old: pd.DataFrame, new: pd.DataFrame) -> np.ndarray:
    """Per row (the two frames are aligned): True when a substantive attribute,
    the geometry or the reference point changed. Volatile transaction fields
    are not among the attribute columns."""
    differs = np.zeros(len(new), dtype=bool)
    for col in (c for c in new.columns if c not in _NON_ATTR_COLS):
        old_col, new_col = old[col], new[col]
        if col in LIST_FIELDS:
            old_col, new_col = _list_key(old_col), _list_key(new_col)
        differs |= _column_differs(old_col, new_col)
    differs |= _geometry_differs(old["geometry"], new["geometry"])
    differs |= _geometry_differs(old["reference_point"], new["reference_point"])
    return differs


def snapshot_events(
    current: gpd.GeoDataFrame,
    previous: gpd.GeoDataFrame | None,
    municipality_code: str,
    yyyymm: str,
) -> gpd.GeoDataFrame:
    """The events of the month `yyyymm` of one municipality: `current` against
    its predecessor snapshot `previous` (None for the first available month,
    where every object is `added`). A `code` repeated within a snapshot keeps
    its last row."""
    current = current.drop_duplicates("code", keep="last").set_index("code", drop=False)
    previous = (
        None if previous is None
        else previous.drop_duplicates("code", keep="last").set_index("code", drop=False)
    )

    if previous is None:
        parts = [current.assign(event_type=ADDED)]
    else:
        common = current.index.intersection(previous.index)
        in_both_now = current.loc[common]
        changed = _rows_differ(previous.loc[common].reset_index(drop=True),
                               in_both_now.reset_index(drop=True))
        parts = [
            current.loc[current.index.difference(previous.index)].assign(event_type=ADDED),
            in_both_now[changed].assign(event_type=CHANGED),
            previous.loc[previous.index.difference(current.index)].assign(event_type=REMOVED),
        ]

    events = pd.concat(parts, ignore_index=True)
    events.insert(0, "municipality_code", municipality_code)
    events.insert(1, "yyyymm", yyyymm)
    events.insert(2, "event_date", current["snapshot_date"].iloc[0])
    events.insert(3, "event_type", events.pop("event_type"))

    events = gpd.GeoDataFrame(events, geometry="geometry", crs=CRS_VFR)
    events["reference_point"] = gpd.GeoSeries(events["reference_point"], crs=CRS_VFR)
    return events.sort_values("code").reset_index(drop=True)


def derive_history(events: pd.DataFrame) -> gpd.GeoDataFrame:
    """The SCD2 history of the objects in `events` (the events of one or more
    municipalities over any months): one row per `added` / `changed` event,
    closed by the next event of the same `code`."""
    ordered = events.sort_values(["code", "yyyymm"], kind="stable")
    by_code = ordered.groupby("code", sort=False)
    next_type = by_code["event_type"].shift(-1)
    next_date = by_code["event_date"].shift(-1)
    if ((next_type == ADDED) & (ordered["event_type"] != REMOVED)).any():
        raise ValueError("an `added` event follows an event of the same object without a `removed` in between")

    keep = ordered["event_type"] != REMOVED
    history = ordered[keep].drop(columns=EVENT_COLUMNS)
    history["valid_from"] = ordered.loc[keep, "event_date"]
    history["valid_to"] = next_date[keep]
    history["end_reason"] = next_type[keep].map(_END_REASON)

    history = gpd.GeoDataFrame(history, geometry="geometry", crs=CRS_VFR)
    history["reference_point"] = gpd.GeoSeries(history["reference_point"], crs=CRS_VFR)
    return history.sort_values(["code", "valid_from"]).reset_index(drop=True)
