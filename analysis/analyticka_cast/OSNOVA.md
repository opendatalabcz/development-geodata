# Analytická část – osnova

Data: `data/clean_output/brno_area_history_clean.parquet` (Brno a okolí, 30 obcí,
vyčištěná verzovaná historie stavebních objektů z RÚIAN, snapshoty 2015-09 až 2026-08).
Číselníky: `src/ruian_history/cleaning/codelists/`. Notebook: `analyza/analyticka_cast/analyticka_cast.ipynb`,
obrázky do `analyza/analyticka_cast/obrazky/`.

## 1. Charakteristika dat
- Krátký odstavec: zdroj (RÚIAN VFR, měsíční snapshoty OB_UKSH), zpracování
  (SCD2 historie, čištění), rozsah (území, období, počet obcí).
- **Tabulka 1 – základní charakteristiky datové sady**: počet verzí záznamů
  (278 171), počet unikátních objektů (124 567), z toho aktuálních (102 220),
  zaniklých (`removed`), počet snapshotů (132), období, počet obcí, podíl Brna.
- **Tabulka 2 – popis použitých atributů**: sloupec, význam, číselník, podíl
  vyplněných hodnot (`floor_count` ~66 %, `built_up_area` ~42 %,
  `completion_date` ~64 %, `usage_type_code` ~93 %).
- Poznámka k úplnosti a známé díře (Brno 2016-05).

## 2. Struktura historie
- Kolik verzí připadá na jeden objekt (histogram: 1 verze, 2, 3, …).
- `end_reason`: změna atributů × zánik objektu.
- **Graf: počet změn a zániků v čase** (po měsících/letech) – ukáže hromadné
  aktualizace RÚIAN a odůvodní časovou osu v aplikaci.
- **Graf: vývoj počtu platných objektů v čase** (stav k datu).

## 3. Typ a využití objektu
- **Graf: typ stavebního objektu** (`building_type_code`: budova s číslem
  popisným/evidenčním, bez čísla, ostatní) – sloupcový graf.
- **Graf: způsob využití** (`usage_type_code`, top ~10 + „ostatní“) – vodorovné
  sloupce; objekt k bydlení, rodinný dům, bytový dům, garáže, …
- Porovnání Brno × okolní obce (skládané sloupce v procentech).
- Podíl objektů bez vyplněného využití.

## 4. Podlažnost a velikost
- **Graf: rozdělení počtu podlaží** (`floor_count`) – histogram, ošetřit extrémy.
- **Graf: zastavěná plocha** (`built_up_area`) – histogram / boxplot v log. měřítku
  (medián ~109 m², dlouhý pravý chvost).
- Podlažnost × způsob využití (boxplot / heatmapa).
- Počet bytů (`unit_count`) u bytových domů.

## 5. Stáří zástavby
- **Graf: objekty podle roku dokončení** (`completion_date`, po dekádách) –
  medián ~1980, výrazné vlny výstavby.
- Stáří × konstrukce (`construction_type_code`): cihla, panel, …
- Pozor na zaokrouhlené datum (31. 12.) u starých objektů.

## 6. Vybavení a technické atributy
- **Graf: přípojky** (voda, kanalizace, plyn), výtah, způsob vytápění –
  malé sloupcové grafy / „small multiples“.
- Rozdíl Brno × okolí (např. plyn, kanalizace).

## 7. Prostorový rozměr
- **Mapa: hustota/počet objektů** po obcích nebo čtvercové síti.
- **Mapa: průměrná podlažnost / rok výstavby** po obcích či částech města.
- **Mapa: nové objekty v posledních letech** (kde se staví – např. Kuřim,
  Šlapanice, Modřice).
- Upozornění na objekty bez geometrie (jen definiční bod).

## 8. Kvalita dat
- Chybějící hodnoty podle atributu (heatmapa chybějících hodnot).
- Podíl oprav při čištění (z `cleaning_log`), typy nalezených chyb.
- Dopad na interpretaci vizualizací.

## 9. Shrnutí a návaznost na aplikaci
- **Tabulka 3 – přehled vizualizací v aplikaci**: vizualizace → použitý atribut →
  typ grafu → otázka, na kterou odpovídá (časová osa, mapa objektů,
  filtr podle využití/podlažnosti/stáří, srovnání obcí, detail objektu
  s historií verzí).
- Co z explorace plyne pro návrh (filtry, výchozí měřítka, ošetření chybějících hodnot).
