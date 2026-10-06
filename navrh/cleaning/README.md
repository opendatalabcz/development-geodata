# Čištění dat

Čistí GeoParquet z pipeliny podle pravidel v [plan_cisteni_dat.md](plan_cisteni_dat.md):

```
python -m navrh.cleaning.clean_history brno_area
```

Vstup je `navrh/output/<jméno>_history.parquet`, výstup
`navrh/clean_output/<jméno>_history_clean.parquet`. Záznam každého zásahu
(smazaný řádek, vynulovaná nebo doplněná hodnota) jde do
`navrh/cleaning/logs/<jméno>_cleaning_log.csv`.

## Souřadnicové systémy

Výstup má geometrii dvakrát:

| sloupce | CRS | k čemu |
|---|---|---|
| `geometry`, `reference_point` | EPSG:5514 (S-JTSK) | výpočty – plochy a vzdálenosti jsou v metrech |
| `geometry_wgs84`, `reference_point_wgs84` | EPSG:4326 (WGS 84) | zobrazení na webové mapě – zeměpisná šířka a délka ve stupních |

S-JTSK je český souřadnicový systém v metrech, takže se v něm dá počítat plocha
a vzdálenost. WGS 84 je světový standard (GPS, Leaflet, MapLibre), ale jeho
jednotkou jsou stupně, takže plochy a vzdálenosti se v něm počítat nemají.
`_wgs84` sloupce jsou jen kopie pro zobrazení; kde chybí `geometry`, chybí i
`geometry_wgs84`.
