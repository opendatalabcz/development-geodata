"""Parsing of RÚIAN VFR XML files of type OB_UKSH (building objects, full
complete dataset) into a geopandas.GeoDataFrame with real geometry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from lxml import etree
from shapely.geometry import MultiPolygon, Point, Polygon

GML_NS = "{http://www.opengis.net/gml/3.2}"
# S-JTSK / Krovak East North
CRS_VFR = "EPSG:5514"

# key = resulting column name (English, snake_case), value = the element name
# per the VFR schema
SCALAR_FIELDS = {
    "code": "Kod",
    "building_type_code": "TypStavebnihoObjektuKod",
    "usage_type_code": "ZpusobVyuzitiKod",
    "record_valid_from": "PlatiOd",
    "completion_date": "Dokonceni",
    "construction_type_code": "DruhKonstrukceKod",
    "unit_count": "PocetBytu",
    "floor_count": "PocetPodlazi",
    "sewage_connection_code": "PripojeniKanalizaceKod",
    "gas_connection_code": "PripojeniPlynKod",
    "water_connection_code": "PripojeniVodovodKod",
    "elevator_code": "VybaveniVytahemKod",
    "built_up_area": "ZastavenaPlocha",
    "heating_type_code": "ZpusobVytapeniKod",
    "change_proposal_global_id": "GlobalniIdNavrhuZmeny",
    "transaction_id": "IdTransakce",
    "iskn_building_id": "IsknBudovaId",
}

INTEGER_FIELDS = ("unit_count", "floor_count")
FLOAT_FIELDS = ("built_up_area",)
DATE_FIELDS = ("record_valid_from", "completion_date", "snapshot_date", "valid_from", "valid_to")
LIST_FIELDS = ("house_numbers", "parcel_ids")

# attributes that change on the same object without a substantive change
# (transaction metadata) - ignored when diffing
VOLATILE_FIELDS = ("record_valid_from", "change_proposal_global_id", "transaction_id")


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _text(el) -> str | None:
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t or None


def _poslist_to_array(text: str) -> np.ndarray:
    """Parse a gml:posList into an (n, 2) float array (a stray odd value is dropped)."""
    values = np.array(text.split(), dtype=float)
    return values[: len(values) // 2 * 2].reshape(-1, 2)


def _polygon_rings(polygon_el) -> list[np.ndarray] | None:
    """The exterior ring followed by the interior rings of a gml:Polygon, as
    coordinate arrays (None when there is no exterior)."""
    ext = polygon_el.find(f"{GML_NS}exterior/{GML_NS}LinearRing/{GML_NS}posList")
    if ext is None or not ext.text:
        return None
    rings = [_poslist_to_array(ext.text)]
    for ring in polygon_el.findall(f"{GML_NS}interior/{GML_NS}LinearRing/{GML_NS}posList"):
        if ring.text:
            rings.append(_poslist_to_array(ring.text))
    return rings


def _boundary_polygons(boundary_el) -> list[list[np.ndarray]]:
    """The ring lists of all polygons in soi:OriginalniHranice."""
    if boundary_el is None:
        return []
    polygons = (_polygon_rings(pe) for pe in boundary_el.iter(f"{GML_NS}Polygon"))
    return [p for p in polygons if p is not None]


def _reference_point_coords(point_el) -> tuple[float, float] | None:
    """The (x, y) of soi:DefinicniBod (gml:Point/gml:pos)."""
    if point_el is None:
        return None
    pos = point_el.find(f".//{GML_NS}pos")
    if pos is None or not pos.text:
        return None
    parts = pos.text.split()
    if len(parts) < 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _build_polygons(polygon_rings: list[list[np.ndarray]]) -> list[Polygon | None]:
    """Build one Polygon per ring list in a single batch call. If any ring is
    invalid (e.g. fewer than 4 coordinates) the batch raises, and the polygons
    are built one by one instead, with the bad ones becoming None. Invalid but
    constructible polygons are kept on purpose - repairing them is up to the
    cleaning."""
    if not polygon_rings:
        return []
    rings = [r for rs in polygon_rings for r in rs]
    ring_ids = np.repeat(np.arange(len(rings)), [len(r) for r in rings])
    polygon_ids = np.repeat(np.arange(len(polygon_rings)), [len(rs) for rs in polygon_rings])
    try:
        linear_rings = shapely.linearrings(np.concatenate(rings), indices=ring_ids)
        return list(shapely.polygons(linear_rings, indices=polygon_ids))
    except (ValueError, shapely.errors.ShapelyError):
        built = []
        for rs in polygon_rings:
            try:
                built.append(Polygon(rs[0], rs[1:]))
            except Exception:
                built.append(None)
        return built


def _build_boundaries(boundaries: list[list[list[np.ndarray]]]) -> list[Polygon | MultiPolygon | None]:
    """Turn per-object lists of polygon ring lists into one Polygon /
    MultiPolygon (or None) per object."""
    flat = [p for polygons in boundaries for p in polygons]
    owner = np.repeat(np.arange(len(boundaries)), [len(p) for p in boundaries])
    per_object: list[list[Polygon]] = [[] for _ in boundaries]
    for obj, poly in zip(owner, _build_polygons(flat)):
        if poly is not None and not poly.is_empty:
            per_object[obj].append(poly)

    result: list[Polygon | MultiPolygon | None] = []
    for polys in per_object:
        if not polys:
            result.append(None)
        elif len(polys) == 1:
            result.append(polys[0])
        else:
            result.append(MultiPolygon(polys))
    return result


def _build_points(coords: list[tuple[float, float] | None]) -> list[Point | None]:
    arr = np.array([c if c is not None else (np.nan, np.nan) for c in coords], dtype=float).reshape(-1, 2)
    present = ~np.isnan(arr[:, 0])
    points = np.full(len(coords), None, dtype=object)
    points[present] = shapely.points(arr[present])
    return list(points)


def _house_numbers(el) -> list[str] | None:
    values = [
        _text(c.find("*"))
        for c in el.findall("*")
        if _local(c.tag) == "CislaDomovni"
    ]
    return [v for v in values if v] or None


def _parcel_ids(el) -> list[str] | None:
    ids = []
    for c in el:
        if _local(c.tag) != "IdentifikacniParcela":
            continue
        for sub in c:
            if _local(sub.tag) == "Id":
                ids.append(_text(sub))
    return [i for i in ids if i] or None


def _district_code(el) -> str | None:
    for c in el:
        if _local(c.tag) == "CastObce":
            for sub in c:
                if _local(sub.tag) == "Kod":
                    return _text(sub)
    return None


def _building_object_to_row(el) -> dict:
    row: dict = {"gml_id": el.get(f"{GML_NS}id")}

    by_local = {}
    for c in el:
        by_local.setdefault(_local(c.tag), []).append(c)

    for col_name, local_name in SCALAR_FIELDS.items():
        els = by_local.get(local_name)
        row[col_name] = _text(els[0]) if els else None

    row["house_numbers"] = _house_numbers(el)
    row["parcel_ids"] = _parcel_ids(el)
    row["district_code"] = _district_code(el)

    geometry_els = by_local.get("Geometrie")
    point = None
    boundary: list[list[np.ndarray]] = []
    if geometry_els:
        geom_el = geometry_els[0]
        for c in geom_el:
            local_name = _local(c.tag)
            if local_name == "DefinicniBod":
                point = _reference_point_coords(c)
            elif local_name == "OriginalniHranice":
                boundary = _boundary_polygons(c)

    # raw coordinates; parse_building_objects turns them into shapely objects in batch
    row["reference_point"] = point
    row["geometry"] = boundary
    return row


def coerce_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Give the columns their final types (the single definition of the schema
    for the attributes; codes and identifiers stay strings). Safe to call on
    data that already has the types. Dates are truncated to midnight; values
    that cannot be converted become missing."""
    df = df.copy()
    for col in DATE_FIELDS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").astype("datetime64[us]").dt.normalize()
    for col in INTEGER_FIELDS:
        if col in df.columns:
            num = pd.to_numeric(df[col], errors="coerce")
            df[col] = num.where(num == num.round()).astype("Int32")
    for col in FLOAT_FIELDS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    for col in LIST_FIELDS:
        if col in df.columns:
            df[col] = df[col].map(_to_list).astype(object)
    return df


def _to_list(v) -> list | None:
    if isinstance(v, str):  # legacy "a; b" strings
        return v.split("; ")
    if isinstance(v, (list, tuple, np.ndarray)):
        return list(v) or None
    return None if pd.isna(v) else [v]


@dataclass
class VfrHeader:
    vfr_version: str | None
    batch_type: str | None
    file_type: str | None
    date: pd.Timestamp | None


def _header_from_root(root) -> VfrHeader:
    ns = root.nsmap.copy()
    header_el = root.find("vf:Hlavicka", ns)
    info = {_local(c.tag): _text(c) for c in header_el if isinstance(c.tag, str)}
    return VfrHeader(
        vfr_version=info.get("VerzeVFR"),
        batch_type=info.get("TypDavky"),
        file_type=info.get("TypSouboru"),
        date=pd.to_datetime(info.get("Datum"), errors="coerce"),
    )


def parse_header(xml_path: str | Path) -> VfrHeader:
    tree = etree.parse(str(xml_path))
    return _header_from_root(tree.getroot())


def parse_building_objects(xml_source, snapshot_date=None, source_name: str | None = None) -> gpd.GeoDataFrame:
    """Parse StavebniObjekty from a VFR XML (OB_UKSH) into a GeoDataFrame."""
    if isinstance(xml_source, (str, Path)):
        xml_path = Path(xml_source)
        tree = etree.parse(str(xml_path))
        source_name = source_name or xml_path.name
    else:
        tree = etree.parse(xml_source)
        source_name = source_name or "?"

    root = tree.getroot()
    ns = root.nsmap.copy()

    if snapshot_date is None:
        snapshot_date = _header_from_root(root).date

    data_el = root.find("vf:Data", ns)
    building_object_els = data_el.findall("vf:StavebniObjekty/vf:StavebniObjekt", ns)

    rows = [_building_object_to_row(el) for el in building_object_els]
    point_coords = [row.pop("reference_point") for row in rows]
    boundaries = [row.pop("geometry") for row in rows]
    if not rows:
        cols = ["gml_id", *SCALAR_FIELDS, "house_numbers", "parcel_ids", "district_code"]
        df = pd.DataFrame(columns=cols)
    else:
        df = pd.DataFrame(rows)
    df["reference_point"] = pd.Series(_build_points(point_coords), index=df.index, dtype=object)
    df["geometry"] = pd.Series(_build_boundaries(boundaries), index=df.index, dtype=object)
    df["snapshot_date"] = snapshot_date
    df["source_file"] = source_name
    df = coerce_dtypes(df)

    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=CRS_VFR)
    gdf["reference_point"] = gpd.GeoSeries(gdf["reference_point"], crs=CRS_VFR)
    return gdf
