"""Pipeline pro zpracování RÚIAN VFR souborů (stavební objekty).

Moduly:
    vfr_parser   – parsování VFR XML (OB_UKSH) do GeoDataFrame se skutečnou
                   geometrií (Point / Polygon / MultiPolygon)
    changefile   – porovnání dvou po sobě jdoucích snapshotů a sestavení
                   změnového souboru (nové / zaniklé / změněné objekty)
    export_gpkg  – uložení snapshotů a změnového souboru do GeoPackage
"""
