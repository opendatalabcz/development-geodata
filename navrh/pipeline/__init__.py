"""Pipeline for processing RÚIAN VFR files (building objects).

Modules:
    vfr_parser     - VFR XML (OB_UKSH) -> GeoDataFrame with real geometry
    download       - monthly OB_UKSH files from services.cuzk.gov.cz/vfr
    history        - HistoryState: versioned history (SCD2) from the snapshots
    build_region   - the entry point: downloads and processes a month range
    regions        - named lists of municipalities for batch runs
    export_parquet - saving into GeoParquet (attributes + geometry in one table)
    export_tabular - CSV (attributes) + GeoJSON (geometry), joined on `code`
    _util          - shared helpers for the export

Subfolders:
    demo - scripts for demo/presentation outputs
    old  - previous iterations, kept for reference
"""
