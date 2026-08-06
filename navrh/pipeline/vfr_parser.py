"""Parsování RÚIAN VFR XML souborů typu OB_UKSH (stavební objekty, úplná
kompletní datová sada) do geopandas.GeoDataFrame se skutečnou geometrií.

Na rozdíl od dřívějšího prototypu v analyza/*.ipynb (funkce flatten_element),
tady se GML geometrie (gml:MultiSurface / gml:Polygon / exterior / interior)
skládá do opravdových shapely objektů – nezplošťuje se do prostého seznamu
souřadnic, takže se zachovávají díry v polygonu i skutečné multipolygony.

Souřadnicový systém dat je EPSG:5514 (S-JTSK / Krovak East North).
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

# Atributy StavebniObjekt, které bereme jako skalární sloupce (mimo geometrii).
# Klíč = výsledný název sloupce, hodnota = lokální jméno XML elementu.
SCALAR_FIELDS = {
    "Kod": "Kod",
    "TypStavebnihoObjektuKod": "TypStavebnihoObjektuKod",
    "ZpusobVyuzitiKod": "ZpusobVyuzitiKod",
    "PlatiOd": "PlatiOd",
    "Dokonceni": "Dokonceni",
    "DruhKonstrukceKod": "DruhKonstrukceKod",
    "PocetBytu": "PocetBytu",
    "PocetPodlazi": "PocetPodlazi",
    "PripojeniKanalizaceKod": "PripojeniKanalizaceKod",
    "PripojeniPlynKod": "PripojeniPlynKod",
    "PripojeniVodovodKod": "PripojeniVodovodKod",
    "VybaveniVytahemKod": "VybaveniVytahemKod",
    "ZastavenaPlocha": "ZastavenaPlocha",
    "ZpusobVytapeniKod": "ZpusobVytapeniKod",
    "GlobalniIdNavrhuZmeny": "GlobalniIdNavrhuZmeny",
    "IdTransakce": "IdTransakce",
    "IsknBudovaId": "IsknBudovaId",
}

NUMERIC_FIELDS = ("PocetBytu", "PocetPodlazi", "ZastavenaPlocha")
DATE_FIELDS = ("PlatiOd", "Dokonceni")

# Atributy, které se u "stejného" objektu běžně mění bez skutečné věcné změny
# (transakční metadata) – při diffování je ignorujeme, viz changefile.py.
VOLATILE_FIELDS = ("PlatiOd", "GlobalniIdNavrhuZmeny", "IdTransakce")


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


def parse_original_hranice(hranice_el) -> Polygon | MultiPolygon | None:
    """Převede soi:OriginalniHranice na shapely Polygon/MultiPolygon."""
    if hranice_el is None:
        return None

    polygons = [
        p for p in (_parse_polygon_el(pe) for pe in hranice_el.iter(f"{GML_NS}Polygon"))
        if p is not None and p.is_valid and not p.is_empty
    ]
    if not polygons:
        return None
    if len(polygons) == 1:
        return polygons[0]
    return MultiPolygon(polygons)


def parse_definicni_bod(bod_el) -> Point | None:
    """Převede soi:DefinicniBod (gml:Point/gml:pos) na shapely Point."""
    if bod_el is None:
        return None
    pos = bod_el.find(f".//{GML_NS}pos")
    if pos is None or not pos.text:
        return None
    parts = pos.text.split()
    if len(parts) < 2:
        return None
    try:
        return Point(float(parts[0]), float(parts[1]))
    except ValueError:
        return None


def _cisla_domovni(el) -> str | None:
    hodnoty = [
        _text(c.find("*"))
        for c in el.findall("*")
        if _local(c.tag) == "CislaDomovni"
    ]
    hodnoty = [h for h in hodnoty if h]
    return "; ".join(hodnoty) if hodnoty else None


def _identifikacni_parcely(el) -> str | None:
    ids = []
    for c in el:
        if _local(c.tag) != "IdentifikacniParcela":
            continue
        for sub in c:
            if _local(sub.tag) == "Id":
                ids.append(_text(sub))
    ids = [i for i in ids if i]
    return "; ".join(ids) if ids else None


def _cast_obce_kod(el) -> str | None:
    for c in el:
        if _local(c.tag) == "CastObce":
            for sub in c:
                if _local(sub.tag) == "Kod":
                    return _text(sub)
    return None


def _stavebni_objekt_to_row(el) -> dict:
    row: dict = {"_gml_id": el.get(f"{GML_NS}id")}

    by_local = {}
    for c in el:
        by_local.setdefault(_local(c.tag), []).append(c)

    for col_name, local_name in SCALAR_FIELDS.items():
        els = by_local.get(local_name)
        row[col_name] = _text(els[0]) if els else None

    row["CislaDomovni"] = _cisla_domovni(el)
    row["IdentifikacniParcely"] = _identifikacni_parcely(el)
    row["CastObceKod"] = _cast_obce_kod(el)

    geometrie_els = by_local.get("Geometrie")
    bod = hranice = None
    if geometrie_els:
        geom_el = geometrie_els[0]
        for c in geom_el:
            local_name = _local(c.tag)
            if local_name == "DefinicniBod":
                bod = parse_definicni_bod(c)
            elif local_name == "OriginalniHranice":
                hranice = parse_original_hranice(c)

    row["definicni_bod"] = bod
    row["geometry"] = hranice if hranice is not None else bod
    row["ma_polygon"] = hranice is not None
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
class VfrHlavicka:
    verze_vfr: str | None
    typ_davky: str | None
    typ_souboru: str | None
    datum: pd.Timestamp | None


def _hlavicka_from_root(root) -> VfrHlavicka:
    ns = root.nsmap.copy()
    hlavicka = root.find("vf:Hlavicka", ns)
    info = {_local(c.tag): _text(c) for c in hlavicka if isinstance(c.tag, str)}
    return VfrHlavicka(
        verze_vfr=info.get("VerzeVFR"),
        typ_davky=info.get("TypDavky"),
        typ_souboru=info.get("TypSouboru"),
        datum=pd.to_datetime(info.get("Datum"), errors="coerce"),
    )


def parse_hlavicka(xml_path: str | Path) -> VfrHlavicka:
    tree = etree.parse(str(xml_path))
    return _hlavicka_from_root(tree.getroot())


def parse_stavebni_objekty(xml_source, snapshot_datum=None, source_name: str | None = None) -> gpd.GeoDataFrame:
    """Naparsuje StavebniObjekty z VFR XML (OB_UKSH) do GeoDataFrame.

    Parameters
    ----------
    xml_source:
        cesta k VFR XML souboru (str/Path), NEBO file-like objekt s bajty
        (např. `io.BytesIO` staženého a rozbaleného souboru – nic se
        nemusí ukládat na disk)
    snapshot_datum:
        datum snapshotu; pokud None, vezme se z hlavičky souboru (vf:Hlavicka/Datum)
    source_name:
        jméno zdrojového souboru pro sloupec `zdrojovy_soubor`; u cesty se
        odvodí automaticky, u file-like objektu je potřeba dodat
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

    if snapshot_datum is None:
        snapshot_datum = _hlavicka_from_root(root).datum

    data_el = root.find("vf:Data", ns)
    stavebni_objekty = data_el.findall("vf:StavebniObjekty/vf:StavebniObjekt", ns)

    rows = [_stavebni_objekt_to_row(el) for el in stavebni_objekty]
    if not rows:
        cols = ["_gml_id", *SCALAR_FIELDS, "CislaDomovni", "IdentifikacniParcely",
                "CastObceKod", "definicni_bod", "geometry", "ma_polygon"]
        df = pd.DataFrame(columns=cols)
    else:
        df = pd.DataFrame(rows)
    df = _coerce_types(df)
    df["snapshot_datum"] = snapshot_datum
    df["zdrojovy_soubor"] = source_name

    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=CRS_VFR)
    gdf["definicni_bod"] = gpd.GeoSeries(gdf["definicni_bod"], crs=CRS_VFR)
    return gdf
