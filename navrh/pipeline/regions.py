"""Pojmenované regiony (seznamy kódů obcí) pro dávkové běhy pipeliny na víc
obcí najednou – viz `build_region.py`.

Kódy obcí jsou skutečné RÚIAN kódy, zjištěné z veřejného ArcGIS REST API
ČÚZK (https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/12, vrstva
„Obec“), ne odhadnuté podle jména. `BRNO_OKOLI` vznikl takto (2026-08):

    1. dotaz na vrstvu 15 (Okres) -> kódy okresů Brno-město (3702) a
       Brno-venkov (3703)
    2. dotaz na vrstvu 12 (Obec) WHERE okres IN (3702, 3703) -> 188 obcí
       s reálnou geometrií (zjednodušenou přes `maxAllowableOffset`)
    3. těžiště polygonu každé obce, vzdálenost od těžiště Brna (kód 582786)
    4. 30 geograficky nejbližších obcí (Brno + 29 sousedů), seřazeno podle
       vzdálenosti

Přehled: 4 km – Brno samotné, dál Ostopovice, Modřice, Kuřim, Šlapanice atd.
(typické blízké příměstské obce), nejvzdálenější z třicítky ~12 km od centra.
"""

from __future__ import annotations

# (kod_obce, nazev, vzdalenost_od_brna_km) – seřazeno od nejbližší (Brno = 0)
BRNO_OKOLI: list[tuple[str, str, float]] = [
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
    "brno_okoli": BRNO_OKOLI,
}
