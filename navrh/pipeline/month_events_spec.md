# Události změn dělené po měsících - návrh

Stav: koncept pro větev `month-partitioned-history`. Nic z toho zatím není
implementováno.

## 1. Cíl

Nahradit sekvenční, stavové sestavení SCD2 (`HistoryState.ingest`, jedna obec
procházená měsíc po měsíci) **událostmi změn dělenými po měsících**. Každá
událost popisuje, co se v daném měsíci stalo s jedním stavebním objektem, a
neodkazuje na žádný jiný měsíc. Měsíce proto lze zpracovávat paralelně a nic
nemusí držet celou historii v paměti. Historie SCD2 (`valid_from` /
`valid_to` / `end_reason`) se z událostí *odvozuje*.

Mimo rozsah: změna výstupu parseru, čištění ani toho, co se považuje za
změnu. Přepracovaná pipeline zapisuje pouze Parquet (žádné CSV / GeoJSON).

## 2. Definice

- **Snímek (snapshot)** - zparsovaný soubor OB_UKSH jedné obce za jeden měsíc
  (`parse_building_objects`). Určen dvojicí `(municipality_code, yyyymm)`.
- **Předchůdce** snímku - snímek *posledního dostupného* měsíce před ním pro
  stejnou obec. Měsíc, který na serveru nemá soubor, nebo jehož soubor se
  nepodařilo stáhnout či zparsovat (dnešní `skipped_months`), není dostupný;
  další měsíc se pak porovnává s tím před ním. Událost se datuje měsícem, ve
  kterém byla změna poprvé *vidět*, přesně jako dnes v `ingest()`. Prázdný
  snímek (0 objektů) se považuje za nedostupný, protože se dnes fakticky
  ignoruje.
- **Objekt** - určen hodnotou `code`. Pokud snímek obsahuje jeden `code`
  vícekrát, vyhrává poslední řádek (jako v `ingest()`).

## 3. Tabulka událostí

Jeden řádek na událost. Sloupce:

| sloupec | význam |
|---|---|
| `municipality_code` | řetězec |
| `yyyymm` | měsíc, ve kterém byla událost zaznamenána (měsíc zdrojového adresáře); klíč dělení |
| `event_date` | `snapshot_date` snímku v měsíci `yyyymm`; stává se `valid_from` / `valid_to` |
| `event_type` | `added`, `changed` nebo `removed` |
| všechny sloupce záznamu | přesně dnešní sloupce řádku: `gml_id`, `code`, skalární atributy, `house_numbers`, `parcel_ids`, `district_code`, `reference_point`, `geometry`, `snapshot_date`, `source_file` |

`municipality_name` se neukládá; doplňuje se při exportu z `regions.py`.

Typy událostí (a nic jiného):

- `added` - `code` je ve snímku, ale není u předchůdce. Nese nový řádek.
- `changed` - `code` je v obou a liší se podle dnešního pravidla
  `_row_differs`: libovolný nevolatilní atribut, `geometry` nebo
  `reference_point`. Volatilní pole (`record_valid_from`,
  `change_proposal_global_id`, `transaction_id`) se ignorují. Nese **celý nový
  řádek**.
- `removed` - `code` je u předchůdce, ale ve snímku není. Nese **poslední
  známý řádek** beze změny, takže jeho `snapshot_date` / `source_file` jsou
  hodnoty předchůdce. `event_date` je přesto datum měsíce, ve kterém objekt
  zmizel.

Nezměněné objekty událost nevytvářejí. Objekt, který zmizí a později se
vrátí, dá `removed` a později `added` pro stejný `code`. U `added` a
`changed` platí `snapshot_date == event_date`.

Na dvojici `(code, yyyymm)` připadá nejvýše jedna událost.

První dostupný měsíc obce nemá předchůdce, takže všechny jeho objekty jsou
`added` (základní stav).

## 4. Úlohy a dělení na bloky

Jednotkou práce je **úloha (task)**: jedna obec a řada po sobě jdoucích
dostupných měsíců (*blok*) plus předchůdce prvního měsíce bloku. Úloha
zpracovává své měsíce jeden po druhém: zparsuje měsíc, porovná ho s předchozím
a ponechá si jen aktuální. V paměti tak drží **nejvýše dva snímky** jedné
obce, bez ohledu na délku bloku.

Délka bloku je parametr `--chunk-months K`:

- výchozí `K = 12` (zatím odhad, doladí se měřením);
- `K = 0` znamená celé období v jedné úloze (sekvenčně po obci, jako dnes);
- blok stojí `K + 1` parsování (jedno navíc pro předchůdce), režie je tedy
  zhruba `1/K`; menší `K` znamená víc paralelismu u velkých obcí, větší méně
  režie.

**Pozdější doplňování používá stejný mechanismus.** Pozdější běh s novějším
`--start` je jen blok, jehož předchůdcem je poslední zpracovaný měsíc:
předchůdce se stáhne a zparsuje znovu (jedno parsování navíc na obec a
aktualizaci). Nejsou potřeba žádné uložené snímky ani rekonstrukce stavu z
událostí.

Plánování (v hlavním procesu, před jakýmkoli stahováním): pro každou obec se
vezmou měsíce ke zpracování (ne zpracované, ne přeskočené), ponechají se ty,
které mají soubor ve výpisu měsíce, rozdělí se na bloky a předchůdcem každého
bloku se nastaví poslední dostupný měsíc před ním (poslední měsíc předchozího
bloku, nebo poslední zpracovaný měsíc ze stavové tabulky).

Pokud se předchůdce za běhu ukáže jako poškozený, úloha se vrátí k dřívějšímu
dostupnému měsíci, který se zparsovat dá, a porovná s ním; poškozený měsíc se
ohlásí jako přeskočený a nevytvoří žádné události. Výsledek je stejný jako u
sekvenčního sestavení.

## 5. Úložiště

Konečné rozložení, datová sada Parquet dělená po měsících s **jedním
souborem na měsíc obsahujícím všechny obce**:

    data/events/yyyymm=YYYYMM/events.parquet

Řádky v každém souboru jsou seřazeny podle `(municipality_code, code)` a
zapsány s row groupami po obcích, takže čtení jedné obce se řídí statistikami
row group místo procházení celého souboru. Zapisuje se podle stávajících
konvencí `write_geoparquet` (skutečná data, CRS v metadatech souboru).

Úlohy konečné soubory nezapisují. Každá úloha zapíše **jeden přípravný
(staging) soubor** s událostmi všech měsíců svého bloku (včetně sloupce
`yyyymm`):

    data/events/_staging/<run_id>/<municipality_code>_<first>-<last>.parquet

počet přípravných souborů je tedy počet úloh, ne obce x měsíce.

**Komprimace (compaction)** proběhne po dokončení úloh běhu: projde přípravné
soubory po proudech a přerozdělí řádky podle `yyyymm` do konečných souborů
měsíců s omezenou pamětí (jedna row group najednou, ne celý měsíc). Již
existující soubor měsíce se sloučí: řádky obcí z tohoto běhu se nahradí,
ostatní zůstanou. Soubory se zapisují pod dočasným názvem a přejmenovávají;
přípravný adresář se maže až poté, co byly zapsány všechny soubory měsíců.

## 6. Stav a průběh

To, co je hotové, lze obnovit z dat (obec má za měsíc řádky nebo zaznamenaný
výsledek s nula událostmi); vše ostatní je v jedné stavové tabulce,
`data/events/status.parquet`:

| sloupec | význam |
|---|---|
| `municipality_code`, `yyyymm` | klíč |
| `status` | `processed`, `pending` nebo `skipped` (stejný význam jako dnes: neúspěšný měsíc v nejnovějším zveřejněném měsíci nebo po něm zůstává `pending` a zkouší se znovu; dřívější neúspěchy jsou `skipped` a znovu se nezkoušejí až do přestavby) |
| `n_added`, `n_changed`, `n_removed` | počty událostí |
| `error` | chybová zpráva u neúspěšných měsíců |

Úlohy ji nezapisují: vracejí malý záznam o stavu za měsíc a **tabulku zapisuje
hlavní proces** (atomická výměna, po každé dávce dokončených úloh a na
konci). Pokud hlavní proces skončí mezi dokončením úlohy a aktualizací
tabulky, měsíc se prostě zpracuje znovu, což je bezpečné, protože komprimace
nahrazuje řádky podle `(municipality, month)`.

## 7. Výpočet událostí jednoho měsíce

Vstup: snímek měsíce `t` a jeho předchůdce `p` (nebo žádný). Rozdíl je merge
podle `code` s vektorizovanými porovnáními (rovnost skalárních sloupců
odolná vůči NA, porovnání seznamů u `house_numbers` / `parcel_ids`,
`shapely.equals` pro geometrie, odpovídající topologickému `a.equals(b)`
používanému dnes).

## 8. Odvození historie SCD2

Provádí se **po obcích**, aby paměť zůstala omezená: načtou se události jedné
obce napříč všemi měsíci (filtr na `municipality_code`), odvodí se historie,
zapíše se historie této obce a pokračuje se další. Události každého `code` se
seřadí podle `yyyymm`.

- Každá událost `added` / `changed` se stane jedním řádkem historie: její
  sloupce záznamu beze změny, `valid_from = event_date`.
- `valid_to` a `end_reason` pocházejí z **další události stejného code**:
  - žádná -> `valid_to = NULL`, `end_reason = NULL` (otevřený řádek);
  - `changed` -> `valid_to = její event_date`, `end_reason = "change"`;
  - `removed` -> `valid_to = její event_date`, `end_reason = "removed"`.
- Události `removed` nevytvářejí vlastní řádek; hodnoty, které nesou, slouží
  k auditu.

V pandas: seřadit podle `(code, yyyymm)`, pak `groupby("code")` se
`shift(-1)`. Výstupní sloupce i jejich pořadí jsou shodné s dnešní historií,
takže čištění tím není dotčeno. `municipality_name` se doplňuje zde, při
exportu.

### Invarianty (co dnes kontroluje `clean_history.py`)

Všechny platí ze samé konstrukce:

| invariant | proč platí |
|---|---|
| `valid_from` nikdy nechybí | `event_date` je vždy vyplněno |
| `valid_to` null <=> `end_reason` null | obojí pochází ze stejné "další události" |
| `valid_from < valid_to` | další událost je ve striktně pozdějším měsíci |
| `snapshot_date == valid_from` | řádky vznikají z `added` / `changed`, kde jsou shodné |
| žádné duplicitní `(code, valid_from)` | jedna událost na `(code, yyyymm)` |
| nejvýše jeden otevřený řádek na `code` | jen poslední událost kódu může nemít následníka |

## 9. Plán ověření

Odvozená historie se musí řádek po řádku shodovat se stávajícím výstupem
`HistoryState` (řazeno podle `code`, `valid_from`; všechny sloupce, geometrie
porovnané jako WKB) na:

1. Chyně (539309), celý archiv.
2. 29 malých obcí na Brněnsku, rozsah 12 měsíců.
3. Syntetické případy: objekt zrušený a znovu přidaný; měsíc bez souboru;
   poškozený měsíc uprostřed (i když je předchůdcem bloku); prázdný snímek;
   duplicitní `code` ve snímku; změna dotýkající se jen volatilních polí
   (nesmí vytvořit událost).
4. **Nezávislost na velikosti bloku:** stejný vstup s `K = 1`, `12` a `0`
   musí dát shodné události.
5. **Inkrementální = celkové:** sestavení měsíců 1..n jedním během a měsíců
   1..m a pak m+1..n dvěma běhy musí dát shodné události.

## 10. Otevřené otázky

1. Zda `pyarrow.dataset.write_dataset` při přerozdělování zachová metadata
   GeoParquet `geo`; pokud ne, komprimace zapíše soubory měsíců přes
   `ParquetWriter` a metadata připojí sama. Před stavbou na tom je potřeba
   malý test.
2. Cena `--rebuild` pro jednu obec: její řádky je třeba nahradit v každém
   souboru měsíce (až ~133 přepisů). Pravděpodobně přijatelné, k změření.
3. Výchozí hodnota `--chunk-months` (12 je odhad).
4. První měsíc archivu je základní stav (všechny objekty `added`), takže je
   zdaleka největším souborem měsíce.
