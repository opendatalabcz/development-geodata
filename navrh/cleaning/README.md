# Čištění dat

Čistí GeoParquet z pipeliny podle pravidel níže:

```
python -m navrh.cleaning.clean_history brno_area
```

Vstup je `navrh/output/<jméno>_history.parquet`, výstup
`navrh/clean_output/<jméno>_history_clean.parquet`. Záznam každého zásahu
(smazaný řádek, vynulovaná nebo doplněná hodnota) jde do
`navrh/cleaning/logs/<jméno>_cleaning_log.csv`.

## Princip

- nepovinné atributy, co nejsou vyplněné
  (`floor_count`, `built_up_area`, kódy přípojek...), zůstávají `null`
- špatné hodnoty (mimo číselník, mimo rozumný rozsah) vynulovat
- zahození řádku jen kvůli identifikačním sloupcům

## Pravidla po sloupcích

Jako první probíhá kontrola dat z pipeliny - při porušení nastane chyba a běh se přeruší

- **`valid_from`** — nesmí chybět; nesmí být `>= valid_to` u uzavřené verze;
  musí odpovídat `snapshot_date`; nesmí být dřívější než `record_valid_from`;
  součást kontroly duplicit `(code, valid_from)`.
- **`valid_to`** — musí souhlasit s `end_reason` na tom, jestli je verze
  otevřená (buď oboje `null`, nebo oboje vyplněné); je-li vyplněné, musí být
  `> valid_from`.
- **`end_reason`** — musí souhlasit s `valid_to` na otevřenosti verze (viz výše).
- **`snapshot_date`** — musí se rovnat `valid_from` (kontrola běží, jen pokud
  sloupec ve vstupu je).
- **`record_valid_from`** — nesmí být pozdější než `valid_from`.
- **`code`** — žádné duplicitní páry `(code, valid_from)`; u žádného `code`
  nesmí být víc než jedna otevřená verze (`valid_to` = `null`).

### Čištění dat

**`gml_id`, `code`** — musí být vyplněné; `code` musí být kladné celé číslo
(délka se liší - 8 - 9 míst). Neplatné/chybí ⇒ **zahodit řádek**,
zalogovat.

**`municipality_code`, `municipality_name`** — musí být vyplněné. Neplatné/chybí ⇒ **zahodit řádek**,
zalogovat.

**`change_proposal_global_id`, `transaction_id`** — chybí ⇒ **zahodit řádek**

**`building_type_code`, `usage_type_code`, `construction_type_code`,
`sewage_connection_code`, `gas_connection_code`, `water_connection_code`,
`elevator_code`, `heating_type_code`** — kategoriální kódy s číselníkem. Mimo číselník ⇒ **vynulovat hodnotu**, zalogovat.

**`unit_count`** —  rozsah 0–1000. Mimo ⇒ **vynulovat**.

**`floor_count`** —  rozsah 0–40. Mimo ⇒ **vynulovat**.

**`built_up_area`** —  rozsah 0–100 000 m². Mimo ⇒ **vynulovat**.

**`district_code`** —  kontrola > 0. Mimo ⇒
**vynulovat**.

**`completion_date`** — rozsah [1000, dnes], nesmí být v budoucnosti.
Mimo ⇒ **vynulovat**. 

**`iskn_building_id`, `house_numbers`, `parcel_ids`** — volitelné,
nekontroluje se.

## Doplňování `completion_date`

Typy mezer/nesrovnalostí
- jedno známé datum, v některé verzi chybí - doplnit
- více datumů u stejné budovy, v některé verzi chybí:
  - novější datum dokončení (chronologicky) 
  - starší datum dokončení (retrospektivní) 

Postup (podle `code`, verze seřazené podle `valid_from`):

- Pro verzi s chybějící `completion_date` se hledá hodnota z **nejbližší
  starší verze** téhož objektu, která už svou `completion_date` má. Pokud
  žádná starší verze hodnotu nemá, použije se hodnota z **nejbližší novější**
  verze.
- V obou případech platí podmínka: doplněná hodnota nesmí být pozdější než
  `valid_from` dané verze (objekt by byl dokončený dřív, než mohl). Nesplňuje-li
  nic, zůstává `NA`.
- Verze, co už svou hodnotu má, se nepřepisuje, i kdyby pozdější oprava
  naznačovala, že je špatně.

Příklad:
- 3 verze: NA, 2000, NA -> 2000, 2000, 2000
- 2 verze: NA (valid from 2016), 2017 -> NA, 2017
- 5 verzí (novější datum dokončení, rekonstrukce): NA, 1998, NA, 2020, NA
  -> 1998, 1998, 1998, 2020, 2020
- 5 verzí (starší datum dokončení, retrospektivní upřesnění): NA, 1990, NA, 1985, NA
  -> 1990, 1990, 1990, 1985, 1985 (oprava na 1985 platí až od verze, kde byla
  zjištěna, dřívější prázdné verze zůstávají u původní hodnoty 1990)

## Log

CSV se záznamem každého zásahu. Sloupce:
`code`, `row_index`, `column`, `old_value`, `new_value`, `action`
(`dropped_row` / `nulled_value` / `filled_value` / `left_null`),
`reason` (lidsky čitelný důvod).

## Geometrie - souřadnicové systémy

Výstup má geometrii dvakrát:

| sloupce | CRS | k čemu |
|---|---|---|
| `geometry`, `reference_point` | EPSG:5514 (S-JTSK) | výpočty – plochy a vzdálenosti jsou v metrech |
| `geometry_wgs84`, `reference_point_wgs84` | EPSG:4326 (WGS 84) | zobrazení na webové mapě – zeměpisná šířka a délka ve stupních |

S-JTSK je český souřadnicový systém v metrech, takže se v něm dá počítat plocha a vzdálenost.
WGS 84 je světový standard využívaný např. GPS, ale jeho jednotkou jsou stupně, takže plochy a vzdálenosti se v něm počítat nedají.
