"""Parsing of RÚIAN VFR XML files of type OB_UKSH (building objects, full
complete dataset) into a geopandas.GeoDataFrame with real geometry.

Unlike the earlier prototype in analyza/*.ipynb (function flatten_element),
here the GML geometry (gml:MultiSurface / gml:Polygon / exterior / interior)
is assembled into actual shapely objects - it is not flattened into a plain
list of coordinates, so polygon holes and true multipolygons are preserved.

The coordinate system of the data is EPSG:5514 (S-JTSK / Krovak East North).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pandas as pd
from lxml import etree
from shapely.geometry import MultiPolygon, Point, Polygon

GML_NS = "{http://www.opengis.net/gml/3.2}"
CRS_VFR = "EPSG:5514"

# StavebniObjekt attributes taken as scalar columns (geometry aside).
# Key = resulting column name (English, snake_case), value = local name of the
# XML element per the VFR schema (that one stays unchanged).
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

NUMERIC_FIELDS = ("unit_count", "floor_count", "built_up_area")
DATE_FIELDS = ("record_valid_from", "completion_date")

# Attributes that routinely change on "the same" object without any real
# substantive change (transaction metadata) - ignored when diffing, see
# changefile.py.
VOLATILE_FIELDS = ("record_valid_from", "change_proposal_global_id", "transaction_id")


def _local(tag: str) -> str:
    return etree.QName(tag).localname


def _text(el) -> str | None:
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t or None


def _poslist_to_coords(text: str) -> list[tuple[float, float]]:
    values = [float(v) for v in text.split()]
    return list(zip(values[0::2], values[1::2]))


def _parse_polygon_el(polygon_el) -> Polygon | None:
    ext = polygon_el.find(f"{GML_NS}exterior/{GML_NS}LinearRing/{GML_NS}posList")
    if ext is None or not ext.text:
        return None
    exterior = _poslist_to_coords(ext.text)

    interiors = []
    for ring in polygon_el.findall(f"{GML_NS}interior/{GML_NS}LinearRing/{GML_NS}posList"):
        if ring.text:
            interiors.append(_poslist_to_coords(ring.text))

    try:
        return Polygon(exterior, interiors)
    except Exception:
        return None


def parse_boundary(boundary_el) -> Polygon | MultiPolygon | None:
    """Convert soi:OriginalniHranice into a shapely Polygon/MultiPolygon."""
    if boundary_el is None:
        return None

    polygons = [
        p for p in (_parse_polygon_el(pe) for pe in boundary_el.iter(f"{GML_NS}Polygon"))
        if p is not None and p.is_valid and not p.is_empty
    ]
    if not polygons:
        return None
    if len(polygons) == 1:
        return polygons[0]
    return MultiPolygon(polygons)


def parse_reference_point(point_el) -> Point | None:
    """Convert soi:DefinicniBod (gml:Point/gml:pos) into a shapely Point."""
    if point_el is None:
        return None
    pos = point_el.find(f".//{GML_NS}pos")
    if pos is None or not pos.text:
        return None
    parts = pos.text.split()
    if len(parts) < 2:
        return None
    try:
        return Point(float(parts[0]), float(parts[1]))
    except ValueError:
        return None


def _house_numbers(el) -> str | None:
    values = [
        _text(c.find("*"))
        for c in el.findall("*")
        if _local(c.tag) == "CislaDomovni"
    ]
    values = [v for v in values if v]
    return "; ".join(values) if values else None


def _parcel_ids(el) -> str | None:
    ids = []
    for c in el:
        if _local(c.tag) != "IdentifikacniParcela":
            continue
        for sub in c:
            if _local(sub.tag) == "Id":
                ids.append(_text(sub))
    ids = [i for i in ids if i]
    return "; ".join(ids) if ids else None


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
    point = boundary = None
    if geometry_els:
        geom_el = geometry_els[0]
        for c in geom_el:
            local_name = _local(c.tag)
            if local_name == "DefinicniBod":
                point = parse_reference_point(c)
            elif local_name == "OriginalniHranice":
                boundary = parse_boundary(c)

    row["reference_point"] = point
    row["geometry"] = boundary if boundary is not None else point
    row["has_polygon"] = boundary is not None
    return row


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in DATE_FIELDS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in NUMERIC_FIELDS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


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
    """Parse StavebniObjekty from a VFR XML (OB_UKSH) into a GeoDataFrame.

    Parameters
    ----------
    xml_source:
        path to the VFR XML file (str/Path), OR a file-like object of bytes
        (e.g. `io.BytesIO` of a downloaded and decompressed file - nothing has
        to be written to disk)
    snapshot_date:
        date of the snapshot; if None, taken from the file header
        (vf:Hlavicka/Datum)
    source_name:
        name of the source file for the `source_file` column; derived
        automatically from a path, has to be supplied for a file-like object
    """
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
    if not rows:
        cols = ["gml_id", *SCALAR_FIELDS, "house_numbers", "parcel_ids",
                "district_code", "reference_point", "geometry", "has_polygon"]
        df = pd.DataFrame(columns=cols)
    else:
        df = pd.DataFrame(rows)
    df = _coerce_types(df)
    df["snapshot_date"] = snapshot_date
    df["source_file"] = source_name

    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=CRS_VFR)
    gdf["reference_point"] = gpd.GeoSeries(gdf["reference_point"], crs=CRS_VFR)
    return gdf
