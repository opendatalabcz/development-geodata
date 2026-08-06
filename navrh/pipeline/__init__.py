"""Pipeline pro zpracování RÚIAN VFR souborů (stavební objekty).

Aktuální moduly:
    vfr_parser     – parsování VFR XML (OB_UKSH) do GeoDataFrame se skutečnou
                     geometrií (Point / Polygon / MultiPolygon)
    download       – stahování měsíčních OB_UKSH souborů ze
                     services.cuzk.gov.cz/vfr po jedné obci, v paměti
    history        – HistoryState: verzovaná historie (SCD2) sestavená
                     průběžně z posloupnosti měsíčních snapshotů
    build_pilot_obec – hlavní orchestrace: stažení + zpracování celé
                     dostupné historie jedné obce
    export_gpkg    – uložení do GeoPackage
    export_tabular – export do CSV (atributy) + GeoJSON (geometrie),
                     propojitelné přes sloupec `Kod`
    _util          – sdílené pomocné funkce pro export

Podsložky:
    demo – skripty jen pro přípravu demo/prezentačních výstupů
           (nejsou součástí hlavní pipeline)
    old  – předchozí iterace, ponecháno pro referenci (nahrazeno výše)
"""
