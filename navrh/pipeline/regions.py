"""Named regions (lists of municipality codes) for batch runs of the pipeline
over several municipalities at once - see `build_region.py`.

The municipality codes are real RÚIAN codes, looked up in the public ArcGIS
REST API of ČÚZK (https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/12,
layer "Obec"), not guessed from the name. `BRNO_AREA` was built like this
(2026-08):

    1. query layer 15 (Okres/district) -> codes of the districts Brno-město
       (3702) and Brno-venkov (3703)
    2. query layer 12 (Obec/municipality) WHERE okres IN (3702, 3703) -> 188
       municipalities with real geometry (simplified via `maxAllowableOffset`)
    3. centroid of each municipality polygon, distance from the centroid of
       Brno (code 582786)
    4. the 30 geographically closest municipalities (Brno + 29 neighbours),
       sorted by distance

Overview: 4 km - Brno itself, further out Ostopovice, Modřice, Kuřim,
Šlapanice etc. (typical nearby suburban municipalities), the most distant of
the thirty is ~12 km from the centre.
"""

from __future__ import annotations

# (municipality_code, name, distance_from_brno_km) - sorted from the closest
# (Brno = 0)
BRNO_AREA: list[tuple[str, str, float]] = [
    ("582786", "Brno", 0.0),
    ("583596", "Ostopovice", 6.4),
    ("583413", "Moravany", 7.0),
    ("583791", "Rozdrojovice", 7.2),
    ("583171", "Jinačovice", 7.3),
    ("584029", "Troubsko", 7.4),
    ("582824", "Bílovice nad Svitavou", 7.9),
    ("583456", "Nebovidy", 8.0),
    ("582921", "Česká", 8.3),
    ("583197", "Kanice", 8.7),
    ("583669", "Popůvky", 8.8),
    ("583391", "Modřice", 9.1),
    ("583910", "Střelice", 9.3),
    ("583821", "Řícmanice", 9.6),
    ("583286", "Lelekovice", 10.0),
    ("583634", "Podolí", 10.3),
    ("584266", "Želešice", 10.4),
    ("583251", "Kuřim", 10.8),
    ("583952", "Šlapanice", 10.9),
    ("583430", "Moravské Knínice", 11.0),
    ("583405", "Mokrá-Horákov", 11.1),
    ("584151", "Vranov", 11.1),
    ("582794", "Babice nad Svitavou", 11.2),
    ("583545", "Omice", 11.3),
    ("582999", "Hajany", 11.3),
    ("583740", "Radostice", 11.5),
    ("583774", "Rebešovice", 11.7),
    ("583651", "Popovice", 11.7),
    ("583561", "Ořechov", 12.0),
    ("584096", "Velatice", 12.0),
]

REGIONS: dict[str, list[tuple[str, str, float]]] = {
    "brno_area": BRNO_AREA,
}
