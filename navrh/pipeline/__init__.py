"""Pipeline for processing RÚIAN VFR files (building objects).

Current modules:
    vfr_parser     - parsing of VFR XML (OB_UKSH) into a GeoDataFrame with real
                     geometry (Point / Polygon / MultiPolygon)
    download       - downloading of the monthly OB_UKSH files from
                     services.cuzk.gov.cz/vfr, one municipality at a time, in
                     memory
    history        - HistoryState: versioned history (SCD2) built incrementally
                     from a sequence of monthly snapshots
    build_region   - the main orchestration (the single entry point):
                     downloading + processing of the chosen month range for one
                     municipality, a list of municipalities or a whole named
                     region
    regions        - named lists of municipalities for batch runs
    export_gpkg    - saving into GeoPackage
    export_tabular - export into CSV (attributes) + GeoJSON (geometry),
                     joinable through the `code` column
    _util          - shared helpers for the export

Subfolders:
    demo - scripts used only to prepare demo/presentation outputs
           (not part of the main pipeline)
    old  - previous iterations, kept for reference (superseded by the above)
"""
